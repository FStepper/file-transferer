"""Phase 6: parallel prefetch/verification and timeouts for stalled packets."""

import threading
import time

import pytest

import file_transferer.prefetch as prefetch
from file_transferer.manifest import read_manifest
from file_transferer.packet import packet_name
from file_transferer.prefetch import PacketVerifier
from file_transferer.receiver import TransferError, receive_folder, verify_transfer
from file_transferer.sender import SendOptions, send_folder

K, M = 4, 2
OPTS = SendOptions(k=K, m=M, block_size=4096, zstd_level=0)


@pytest.fixture
def transfer(tmp_path):
    src = tmp_path / "Clips"
    src.mkdir()
    (src / "a.mp4").write_bytes(bytes(range(256)) * 600)
    (src / "b.wav").write_bytes(bytes(reversed(range(256))) * 300)
    drive = tmp_path / "My Drive"
    drive.mkdir()
    res = send_folder(src, drive, OPTS)
    return src, res.target, tmp_path / "out"


@pytest.fixture
def hang():
    """Make checks of selected packets block like a stuck Drive placeholder read."""
    release = threading.Event()
    stuck: set[tuple[int, int]] = set()
    real = prefetch.check_packet

    def fake(folder, man, s, i, cancel=None, on_progress=None):
        if (s, i) in stuck:
            release.wait(30)  # no progress callbacks while "hanging"
        return real(folder, man, s, i, cancel, on_progress)

    mp = pytest.MonkeyPatch()
    mp.setattr(prefetch, "check_packet", fake)
    mp.setattr(prefetch, "PACKET_TIMEOUT", 0.3)
    try:
        yield stuck
    finally:
        release.set()
        mp.undo()


def test_checks_run_in_parallel(transfer, monkeypatch):
    src, folder, dest = transfer
    real = prefetch.check_packet
    lock = threading.Lock()
    running, peak = [0], [0]

    def slow(*args, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.02)
        try:
            return real(*args, **kwargs)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(prefetch, "check_packet", slow)
    out = receive_folder(folder, dest, wait=False)
    assert peak[0] > 1
    assert (out.dest / "a.mp4").read_bytes() == (src / "a.mp4").read_bytes()


def test_parity_only_requested_when_needed(transfer, monkeypatch):
    src, folder, dest = transfer
    real = prefetch.check_packet
    seen = []

    def spy(folder_, man, s, i, *a, **kw):
        seen.append((s, i))
        return real(folder_, man, s, i, *a, **kw)

    monkeypatch.setattr(prefetch, "check_packet", spy)
    receive_folder(folder, dest, wait=False)
    assert seen and all(i < K for _, i in seen)


def test_stalled_packet_falls_back_to_parity(transfer, hang):
    src, folder, dest = transfer
    hang.add((0, 0))
    t0 = time.monotonic()
    out = receive_folder(folder, dest, wait=False)
    assert time.monotonic() - t0 < 10
    assert out.repaired_stripes >= 1
    assert (out.dest / "a.mp4").read_bytes() == (src / "a.mp4").read_bytes()
    assert (out.dest / "b.wav").read_bytes() == (src / "b.wav").read_bytes()


def test_many_stalled_packets_do_not_block_the_queue(transfer, hang):
    """More stalled reads than workers: replacement threads keep the queue moving."""
    src, folder, dest = transfer
    for s in range(3):
        hang.update({(s, 0), (s, 1)})       # 6 stuck reads, 4 initial workers
    out = receive_folder(folder, dest, wait=False)
    assert out.repaired_stripes >= 3
    assert (out.dest / "a.mp4").read_bytes() == (src / "a.mp4").read_bytes()


def test_too_many_stalled_packets_fail_without_wait(transfer, hang):
    src, folder, dest = transfer
    hang.update({(1, i) for i in range(M + 1)})
    with pytest.raises(TransferError):
        receive_folder(folder, dest, wait=False)


def test_verify_reports_stalled_packets(transfer, hang):
    src, folder, dest = transfer
    hang.add((2, K))
    v = verify_transfer(folder)
    assert v.stalled_packets == [packet_name(2, K)]
    assert v.degraded == [2] and v.ok


def test_verify_hashes_each_packet_once(transfer, monkeypatch):
    src, folder, dest = transfer
    real = prefetch.check_packet
    counts: dict = {}

    def count(folder_, man, s, i, *a, **kw):
        counts[(s, i)] = counts.get((s, i), 0) + 1
        return real(folder_, man, s, i, *a, **kw)

    monkeypatch.setattr(prefetch, "check_packet", count)
    v = verify_transfer(folder)
    assert v.ok and v.files_checked == 2
    assert counts and max(counts.values()) == 1


def test_transient_read_errors_are_retried(transfer, monkeypatch):
    """More than m packets of a stripe fail once (e.g. faulty RAM); a re-read succeeds."""
    src, folder, dest = transfer
    real = prefetch.check_packet
    flaky = {(1, i) for i in range(K + M)}

    def once_bad(folder_, man, s, i, *a, **kw):
        if (s, i) in flaky:
            flaky.discard((s, i))
            return None
        return real(folder_, man, s, i, *a, **kw)

    monkeypatch.setattr(prefetch, "check_packet", once_bad)
    out = receive_folder(folder, dest, wait=False)
    assert (out.dest / "a.mp4").read_bytes() == (src / "a.mp4").read_bytes()


def test_verify_retries_transient_errors(transfer, monkeypatch):
    src, folder, dest = transfer
    real = prefetch.check_packet
    flaky = {(0, 1), (2, K)}

    def once_bad(folder_, man, s, i, *a, **kw):
        if (s, i) in flaky:
            flaky.discard((s, i))
            return None
        return real(folder_, man, s, i, *a, **kw)

    monkeypatch.setattr(prefetch, "check_packet", once_bad)
    v = verify_transfer(folder)
    assert v.ok and not v.bad_packets and v.intact == v.stripes


def test_corrupt_stream_gives_transfer_error_and_resumes(transfer, monkeypatch):
    """A misread that slips past the packet checks surfaces as TransferError; a second
    receive resumes and succeeds."""
    import file_transferer.receiver as receiver

    src, folder, dest = transfer
    real_read = receiver._StripeStream.read
    state = {"done": False}

    def flip_once(self, size=-1):
        data = real_read(self, size)
        if not state["done"] and self.consumed > 40_000 and data:
            state["done"] = True
            data = bytes([data[0] ^ 1]) + data[1:]
        return data

    monkeypatch.setattr(receiver._StripeStream, "read", flip_once)
    with pytest.raises(TransferError):
        receive_folder(folder, dest, wait=False)
    out = receive_folder(folder, dest, wait=False)
    assert out.resumed
    assert (out.dest / "a.mp4").read_bytes() == (src / "a.mp4").read_bytes()
    assert (out.dest / "b.wav").read_bytes() == (src / "b.wav").read_bytes()


def test_changed_packet_is_checked_again(transfer):
    src, folder, dest = transfer
    man = read_manifest(folder)
    v = PacketVerifier(folder, man)
    try:
        path = folder / packet_name(0, 0)
        good = path.read_bytes()
        path.write_bytes(good[:-1])                 # truncated, e.g. sync still running
        assert 0 not in v.collect(0).valid
        path.write_bytes(good)                      # Drive finished syncing
        assert 0 in v.collect(0).valid
    finally:
        v.close()
