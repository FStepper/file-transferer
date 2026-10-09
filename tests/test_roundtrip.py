import json
import os
import random
import threading
from pathlib import Path

import pytest

import vidtransfer.receiver as receiver
from vidtransfer.manifest import MANIFEST_NAMES, STATUS_IN_PROGRESS, read_manifest, write_manifest
from vidtransfer.packet import packet_name, verify_packet
from vidtransfer.receiver import (
    PART_SUFFIX, RECEIVING_SUFFIX, REPORT_NAME, STATE_NAME, InsufficientSpaceError,
    TransferError, _safe_target, receive_folder, transfer_status, verify_transfer,
)
from vidtransfer.sender import README_NAME, SendOptions, send_folder
from vidtransfer.util import Cancelled

K, M = 4, 2
OPTS = SendOptions(k=K, m=M, block_size=4096, zstd_level=3)


def make_source(root: Path) -> Path:
    rnd = random.Random(42)
    src = root / "Dreh Tag 1 – Kamera Ä"  # non-ASCII on purpose
    (src / "Audio").mkdir(parents=True)
    (src / "Leer").mkdir()
    (src / "clip01.mp4").write_bytes(rnd.randbytes(150_000))          # incompressible
    (src / "clip02.mp4").write_bytes(rnd.randbytes(70_001))
    (src / "Audio" / "ton.wav").write_bytes(bytes(range(256)) * 400)  # compresses well
    (src / "Audio" / "leer.wav").write_bytes(b"")
    (src / ".DS_Store").write_bytes(b"junk")
    return src


def tree(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel == REPORT_NAME:
            continue
        out[rel] = None if p.is_dir() else p.read_bytes()
    return out


@pytest.fixture
def env(tmp_path):
    src = make_source(tmp_path)
    drive = tmp_path / "My Drive"
    drive.mkdir()
    dest = tmp_path / "Empfang"
    return src, drive, dest


def expected_tree(src: Path) -> dict:
    t = tree(src)
    t.pop(".DS_Store")
    return t


def assert_clean(dest: Path) -> None:
    """No leftovers of the receive machinery in a finished destination."""
    names = [p.name for p in dest.rglob("*")]
    assert not any(n.endswith(PART_SUFFIX) for n in names)
    assert STATE_NAME not in names
    assert not any(n.endswith(RECEIVING_SUFFIX) for n in os.listdir(dest.parent))


# ---------------------------------------------------------------- roundtrip

def test_roundtrip(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    assert res.target == drive / src.name
    assert res.stripes > 3
    assert (res.target / README_NAME).exists()
    out = receive_folder(res.target, dest, wait=False)
    assert out.dest == dest / src.name
    assert out.repaired_stripes == 0 and not out.resumed
    assert tree(out.dest) == expected_tree(src)
    assert_clean(out.dest)


@pytest.mark.parametrize("level", [0, 19])
def test_roundtrip_compression_levels(env, level):
    src, drive, dest = env
    res = send_folder(src, drive, SendOptions(k=3, m=1, block_size=8192, zstd_level=level))
    out = receive_folder(res.target, dest, wait=False)
    assert tree(out.dest) == expected_tree(src)


def test_report(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    out = receive_folder(res.target, dest, wait=False)
    text = out.report.read_text(encoding="utf-8")
    assert "VidTransfer receive report" in text
    for f in read_manifest(res.target)["files"]:
        assert f["sha256"] in text and f["path"] in text


# ---------------------------------------------------------------- fault tolerance

def test_survives_m_losses_per_stripe(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    rnd = random.Random(1)
    for s in range(res.stripes):
        victims = rnd.sample(range(K + M), M)
        for i, v in enumerate(victims):
            p = res.target / packet_name(s, v)
            mode = (s + i) % 3
            if mode == 0:
                p.unlink()                                   # missing
            elif mode == 1:
                data = bytearray(p.read_bytes())
                data[len(data) // 2] ^= 0xFF                 # bit error
                p.write_bytes(bytes(data))
            else:
                with open(p, "r+b") as f:                    # truncated
                    f.truncate(os.path.getsize(p) // 3)
    out = receive_folder(res.target, dest, wait=False)
    assert out.repaired_stripes > 0
    assert tree(out.dest) == expected_tree(src)


def test_too_many_losses_fails(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    for i in range(M + 1):
        (res.target / packet_name(1, i)).unlink()
    man, status = transfer_status(res.target)
    assert not status[1].recoverable and status[0].recoverable
    with pytest.raises(TransferError):
        receive_folder(res.target, dest, wait=False)


def test_resume_and_repair_on_sender(env):
    src, drive, dest = env
    first = send_folder(src, drive, OPTS)
    total = first.stripes * (K + M)
    assert first.packets_written == total

    second = send_folder(src, drive, OPTS)
    assert second.target == first.target and second.resumed
    assert second.packets_written == 0 and second.packets_skipped == total

    victim = first.target / packet_name(2, 0)
    data = bytearray(victim.read_bytes())
    data[-1] ^= 1
    victim.write_bytes(bytes(data))
    (first.target / packet_name(0, 1)).unlink()
    assert verify_packet(victim) is None

    third = send_folder(src, drive, OPTS)
    assert third.packets_written == 2
    assert verify_packet(victim) is not None
    out = receive_folder(first.target, dest, wait=False)
    assert tree(out.dest) == expected_tree(src)


def test_changed_source_gets_new_folder(env):
    src, drive, dest = env
    first = send_folder(src, drive, OPTS)
    (src / "neu.mp4").write_bytes(b"x" * 1000)
    second = send_folder(src, drive, OPTS)
    assert second.target == drive / f"{src.name} (2)"
    assert second.transfer_id != first.transfer_id


def test_manifest_redundancy(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    (res.target / MANIFEST_NAMES[0]).unlink()
    (res.target / MANIFEST_NAMES[1]).write_text("{broken", encoding="utf-8")
    out = receive_folder(res.target, dest, wait=False)
    assert tree(out.dest) == expected_tree(src)


def test_receive_twice_creates_unique_dest(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    a = receive_folder(res.target, dest, wait=False)
    b = receive_folder(res.target, dest, wait=False)
    assert a.dest != b.dest and b.dest.name.endswith("(2)")


def test_cancel_send(env):
    src, drive, dest = env
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        send_folder(src, drive, OPTS, cancel=ev)


def test_receive_waits_for_late_packets(env, monkeypatch):
    """Receiving starts before all packets are there (Drive still syncing)."""
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    hidden = res.target.parent / "hidden"
    hidden.mkdir()
    for i in range(K + M):
        name = packet_name(res.stripes - 1, i)
        os.replace(res.target / name, hidden / name)

    waits = []

    def fake_sleep(seconds, cancel):
        waits.append(seconds)
        for p in hidden.iterdir():
            os.replace(p, res.target / p.name)

    monkeypatch.setattr(receiver, "sleep_cancellable", fake_sleep)
    out = receive_folder(res.target, dest, wait=True)
    assert waits
    assert tree(out.dest) == expected_tree(src)


# ---------------------------------------------------------------- unpacking logic

def test_resume_after_cancelled_receive(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    ev = threading.Event()

    def cancel_after_first_file(done, total, msg):
        if msg.startswith("Entpackt:"):
            ev.set()

    with pytest.raises(Cancelled):
        receive_folder(res.target, dest, cancel_after_first_file, cancel=ev, wait=False)

    partial = dest / (src.name + RECEIVING_SUFFIX)
    state = json.loads((partial / STATE_NAME).read_text(encoding="utf-8"))
    assert len(state["done"]) == 1
    assert not (dest / src.name).exists()

    out = receive_folder(res.target, dest, wait=False)
    assert out.resumed and out.skipped_files == 1
    assert out.dest == dest / src.name
    assert tree(out.dest) == expected_tree(src)
    assert_clean(out.dest)


def test_resume_rewrites_file_changed_in_between(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    ev = threading.Event()

    def cancel_after_first_file(done, total, msg):
        if msg.startswith("Entpackt:"):
            ev.set()

    with pytest.raises(Cancelled):
        receive_folder(res.target, dest, cancel_after_first_file, cancel=ev, wait=False)
    partial = dest / (src.name + RECEIVING_SUFFIX)
    rel = next(iter(json.loads((partial / STATE_NAME).read_text(encoding="utf-8"))["done"]))
    (partial / rel).write_bytes(b"tampered")

    out = receive_folder(res.target, dest, wait=False)
    assert out.skipped_files == 0
    assert tree(out.dest) == expected_tree(src)


def test_hashes_unknown_until_final_manifest(env, monkeypatch):
    """While the sender is still running, file hashes are not yet known: files stay
    as .vtpart and only get their real name once the final manifest arrives."""
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    final_manifests = {n: (res.target / n).read_bytes() for n in MANIFEST_NAMES}
    man = read_manifest(res.target)
    in_progress = {k: v for k, v in man.items()
                   if k not in ("stripes", "stripe_lens", "stream_bytes", "packets", "completed_at")}
    in_progress["status"] = STATUS_IN_PROGRESS
    in_progress["files"] = [{"path": f["path"], "size": f["size"]} for f in man["files"]]
    write_manifest(res.target, in_progress)

    seen_parts = []

    def fake_sleep(seconds, cancel):
        partial = dest / (src.name + RECEIVING_SUFFIX)
        seen_parts.extend(p.name for p in partial.rglob("*" + PART_SUFFIX))
        for n, raw in final_manifests.items():
            (res.target / n).write_bytes(raw)

    monkeypatch.setattr(receiver, "sleep_cancellable", fake_sleep)
    out = receive_folder(res.target, dest, wait=True)
    assert seen_parts
    assert tree(out.dest) == expected_tree(src)
    assert_clean(out.dest)


def test_insufficient_space(env, monkeypatch):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    monkeypatch.setattr(receiver, "free_space", lambda p: 1000)
    with pytest.raises(InsufficientSpaceError):
        receive_folder(res.target, dest, wait=False)
    assert list(dest.iterdir()) == []


@pytest.mark.parametrize("name", ["../evil", "/abs/path", "a/../../b", "C:/x", STATE_NAME, ""])
def test_unsafe_paths_rejected(tmp_path, name):
    assert _safe_target(tmp_path, name) is None


# ---------------------------------------------------------------- verify only

def test_verify_ok(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    v = verify_transfer(res.target)
    assert v.ok and v.complete and v.intact == res.stripes
    assert v.files_checked == 4 and not v.bad_packets
    assert not dest.exists()


def test_verify_degraded_and_unrecoverable(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    for i in range(M):
        (res.target / packet_name(0, i)).unlink()
    v = verify_transfer(res.target)
    assert v.ok and v.degraded == [0] and len(v.bad_packets) == M

    (res.target / packet_name(2, K)).unlink()
    for i in range(M):
        (res.target / packet_name(2, i)).unlink()
    v = verify_transfer(res.target)
    assert not v.ok and v.unrecoverable == [2] and not v.content_verified
