"""Phase 7: container format v2 (records, per-file compression, index, resume jump)."""

import io
import random
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import file_transferer.prefetch as prefetch
import file_transferer.sender as sender
from file_transferer.container import (
    T_DIR, T_END, T_FILE, ContainerError, RecordHeader, RecordReader, RecordWriter, SourceChangedError,
)
from file_transferer.manifest import STATUS_IN_PROGRESS, read_manifest
from file_transferer.packet import HEADER_SIZE, data_len, packet_name, read_header
from file_transferer.receiver import receive_folder
from file_transferer.sender import SendOptions, send_folder
from file_transferer.util import Cancelled

K, M = 4, 2
OPTS = SendOptions(k=K, m=M, block_size=4096, zstd_level=3)
STRIPE = K * 4096


class _Sink(io.BytesIO):
    @property
    def total(self) -> int:
        return self.tell()


class _Source:
    """Minimal stream for RecordReader (read + pos + skip_to)."""

    def __init__(self, data: bytes):
        self.buf = io.BytesIO(data)

    @property
    def pos(self) -> int:
        return self.buf.tell()

    def read(self, n: int) -> bytes:
        return self.buf.read(n)

    def skip_to(self, target: int) -> None:
        self.buf.seek(target)


def rebuild_stream(folder: Path, stripes: int) -> bytes:
    """Concatenate the data packets of all stripes = the container stream."""
    out = bytearray()
    for s in range(stripes):
        hdr = read_header(folder / packet_name(s, 0))
        for i in range(K):
            raw = (folder / packet_name(s, i)).read_bytes()
            out += raw[HEADER_SIZE:HEADER_SIZE + data_len(hdr.stripe_len, K, i)]
    return bytes(out)


@pytest.fixture
def src(tmp_path):
    rnd = random.Random(7)
    s = tmp_path / "Shoot"
    (s / "Audio").mkdir(parents=True)
    (s / "a.mp4").write_bytes(rnd.randbytes(60_000))
    (s / "b.mp4").write_bytes(rnd.randbytes(60_000))
    (s / "c.mov").write_bytes(bytes(50_000))                          # compressible, but a video format
    (s / "Audio" / "ton.wav").write_bytes(bytes(range(256)) * 300)   # compressible
    (s / "Audio" / "noise.wav").write_bytes(rnd.randbytes(40_000))   # not compressible
    return s


@pytest.fixture
def drive(tmp_path):
    d = tmp_path / "My Drive"
    d.mkdir()
    return d


# ---------------------------------------------------------------- per-file compression + index

def test_per_file_compression(src, drive):
    res = send_folder(src, drive, OPTS)
    files = {f["path"]: f for f in read_manifest(res.target)["files"]}
    assert files["a.mp4"]["compression"] == "raw"
    assert files["c.mov"]["compression"] == "raw"            # by extension, despite zeros
    assert files["Audio/ton.wav"]["compression"] == "zstd"
    assert files["Audio/noise.wav"]["compression"] == "raw"  # sample did not shrink


def test_level_zero_stores_everything_raw(src, drive):
    res = send_folder(src, drive, replace(OPTS, zstd_level=0))
    assert {f["compression"] for f in read_manifest(res.target)["files"]} == {"raw"}


def test_index_points_at_records(src, drive):
    res = send_folder(src, drive, OPTS)
    man = read_manifest(res.target)
    stream = rebuild_stream(res.target, res.stripes)
    assert len(stream) == man["stream_bytes"]
    prev_end = 0
    for f in man["files"]:
        assert f["offset"] >= prev_end
        prev_end = f["offset"] + f["length"]
        source = _Source(stream)
        source.skip_to(f["offset"])
        reader = RecordReader(source)
        rec = reader.next()
        assert rec.type == T_FILE and rec.path == f["path"] and rec.size == f["size"]
        assert reader.read_data(rec, lambda _b: None) == f["sha256"]
        assert source.pos == f["offset"] + f["length"]
    for d in man["dirs"]:
        source = _Source(stream)
        source.skip_to(d["offset"])
        assert RecordReader(source).next().type == T_DIR


def test_manifest_is_updated_after_each_stripe(src, drive, monkeypatch):
    snapshots = []
    real = sender.write_manifest

    def spy(folder, data):
        snapshots.append({"status": data["status"],
                          "indexed": sum("offset" in f for f in data["files"])})
        real(folder, data)

    monkeypatch.setattr(sender, "write_manifest", spy)
    res = send_folder(src, drive, OPTS)
    progress = [s for s in snapshots if s["status"] == STATUS_IN_PROGRESS]
    assert len(progress) >= res.stripes            # initial write + one per stripe
    assert any(0 < s["indexed"] < len(read_manifest(res.target)["files"]) for s in progress)


# ---------------------------------------------------------------- receiving

def test_resume_jumps_over_finished_files(src, drive, tmp_path, monkeypatch):
    res = send_folder(src, drive, replace(OPTS, zstd_level=0))
    dest = tmp_path / "out"

    ev = threading.Event()
    finished = []

    def stop_after_three(done, total, msg):
        if msg.startswith("Entpackt:"):
            finished.append(msg)
            if len(finished) == 3:
                ev.set()

    with pytest.raises(Cancelled):
        receive_folder(res.target, dest, stop_after_three, cancel=ev, wait=False)

    man = read_manifest(res.target)
    first_open = man["files"][3]                   # manifest order == stream order
    real = prefetch.check_packet
    stripes_checked = set()

    def spy(folder, man_, s, i, *a, **kw):
        stripes_checked.add(s)
        return real(folder, man_, s, i, *a, **kw)

    monkeypatch.setattr(prefetch, "check_packet", spy)
    out = receive_folder(res.target, dest, wait=False)
    assert out.resumed and out.skipped_files == 3
    assert min(stripes_checked) == first_open["offset"] // STRIPE
    for p in src.rglob("*"):
        if p.is_file():
            assert (out.dest / p.relative_to(src)).read_bytes() == p.read_bytes()


def test_source_growing_during_send_is_detected(src, drive, monkeypatch):
    real_scan = sender.scan_source

    def stale_scan(path):
        entries = real_scan(path)
        for e in entries:
            if e.rel == "a.mp4":
                e.size -= 10                       # file "grew" after it was scanned
        return entries

    monkeypatch.setattr(sender, "scan_source", stale_scan)
    with pytest.raises(SourceChangedError):
        send_folder(src, drive, OPTS)


# ---------------------------------------------------------------- record format

def test_record_roundtrip_and_header_crc(tmp_path):
    f = tmp_path / "x.wav"
    f.write_bytes(bytes(range(256)) * 100)
    sink = _Sink()
    w = RecordWriter(sink, level=3, on_bytes=lambda n: None)
    w.write_dir("Audio", 123)
    entry = w.write_file(f, "Audio/x.wav", f.stat().st_size, 456)
    w.write_end()
    data = sink.getvalue()

    r = RecordReader(_Source(data))
    d = r.next()
    assert (d.type, d.path, d.mtime) == (T_DIR, "Audio", 123)
    rec = r.next()
    assert rec.compressed and rec.offset == entry["offset"]
    got = bytearray()
    assert r.read_data(rec, got.extend) == entry["sha256"]
    assert bytes(got) == f.read_bytes()
    assert r.next().type == T_END

    broken = bytearray(data)
    broken[entry["offset"] + 12] ^= 1             # inside the size field of the file header
    r = RecordReader(_Source(bytes(broken)))
    r.next()
    with pytest.raises(ContainerError):
        r.next()


def test_damaged_file_data_fails_trailer_check(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(random.Random(1).randbytes(10_000))
    sink = _Sink()
    entry = RecordWriter(sink, level=3, on_bytes=lambda n: None).write_file(f, "a.mp4", 10_000, 0)
    data = bytearray(sink.getvalue())
    data[entry["offset"] + 500] ^= 0x10
    r = RecordReader(_Source(bytes(data)))
    rec = r.next()
    with pytest.raises(ContainerError):
        r.read_data(rec, lambda _b: None)


def test_header_pack_is_stable():
    h = RecordHeader(T_FILE, 0, 5, 1700000000, 0o644, "Ä/b.wav").pack()
    assert h == RecordHeader(T_FILE, 0, 5, 1700000000, 0o644, "Ä/b.wav").pack()
    assert h[:4] == b"VTRC"
