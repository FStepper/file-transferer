import os
import random
import threading
from pathlib import Path

import pytest

from vidtransfer.manifest import MANIFEST_NAMES
from vidtransfer.packet import packet_name, verify_packet
from vidtransfer.receiver import TransferError, receive_folder, transfer_status
from vidtransfer.sender import SendOptions, send_folder
from vidtransfer.util import Cancelled

K, M = 4, 2
OPTS = SendOptions(k=K, m=M, block_size=4096, zstd_level=3)


def make_source(root: Path) -> Path:
    rnd = random.Random(42)
    src = root / "Dreh Tag 1 – Kamera Ä"
    (src / "Audio").mkdir(parents=True)
    (src / "Leer").mkdir()
    (src / "clip01.mp4").write_bytes(rnd.randbytes(150_000))          # nicht komprimierbar
    (src / "clip02.mp4").write_bytes(rnd.randbytes(70_001))
    (src / "Audio" / "ton.wav").write_bytes(bytes(range(256)) * 400)  # gut komprimierbar
    (src / "Audio" / "leer.wav").write_bytes(b"")
    (src / ".DS_Store").write_bytes(b"junk")
    return src


def tree(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
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


def test_roundtrip(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    assert res.target == drive / src.name
    assert res.stripes > 3
    assert (res.target / "LIESMICH.txt").exists()
    out = receive_folder(res.target, dest, wait=False)
    assert out.dest == dest / src.name
    assert out.repaired_stripes == 0
    assert tree(out.dest) == expected_tree(src)


@pytest.mark.parametrize("level", [0, 19])
def test_roundtrip_compression_levels(env, level):
    src, drive, dest = env
    res = send_folder(src, drive, SendOptions(k=3, m=1, block_size=8192, zstd_level=level))
    out = receive_folder(res.target, dest, wait=False)
    assert tree(out.dest) == expected_tree(src)


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
                p.unlink()                                   # fehlt
            elif mode == 1:
                data = bytearray(p.read_bytes())
                data[len(data) // 2] ^= 0xFF                 # Bitfehler
                p.write_bytes(bytes(data))
            else:
                with open(p, "r+b") as f:                    # abgeschnitten
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


def test_resume_and_repair(env):
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
    assert (first.target / packet_name(0, 1)).exists()
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
    (res.target / MANIFEST_NAMES[1]).write_text("{kaputt", encoding="utf-8")
    out = receive_folder(res.target, dest, wait=False)
    assert tree(out.dest) == expected_tree(src)


def test_receive_twice_creates_unique_dest(env):
    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    a = receive_folder(res.target, dest, wait=False)
    b = receive_folder(res.target, dest, wait=False)
    assert a.dest != b.dest and b.dest.name.endswith("(2)")


def test_cancel(env):
    src, drive, dest = env
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        send_folder(src, drive, OPTS, cancel=ev)


def test_receive_waits_for_late_packets(env, monkeypatch):
    """Empfang startet, bevor alle Pakete da sind (Drive synchronisiert noch)."""
    import vidtransfer.receiver as receiver

    src, drive, dest = env
    res = send_folder(src, drive, OPTS)
    hidden = res.target.parent / "versteckt"
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
