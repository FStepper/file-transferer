"""Container format v2: a stream of self-describing records.

    stream = record | record | … | end record

    record header (CRC32-protected):
        magic "VTRC", version, type (file/dir/end), flags (raw|zstd), reserved,
        original size (u64), mtime (i64, seconds), mode (u32),
        path length (u16), path (UTF-8, POSIX)
    file data:
        raw:  exactly <original size> bytes
        zstd: chunks of [u32 length][zstd frame of ≤ CHUNK original bytes],
              terminated by a u32 0 – so the sender can stream without knowing
              the compressed size in advance
    file trailer (CRC32-protected):
        magic "VTRE", SHA-256 of the original data, stored data size (u64)

Every file can be verified on its own as soon as its trailer has been read,
and with the offsets from the manifest index a reader can jump straight to any
record. Compared with v1 (one zstd-compressed tar stream), damage only affects
the records it touches.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import zstandard

from file_transferer.util import check_cancel

REC_MAGIC = b"VTRC"
TRAILER_MAGIC = b"VTRE"
REC_VERSION = 1

T_FILE, T_DIR, T_END = 1, 2, 3
F_ZSTD = 0x01

CHUNK = 4 << 20                 # original bytes per zstd frame / read size
SAMPLE = 4 << 20                # bytes used to decide whether compression pays off
MIN_SAVING = 0.03               # compress only if the sample shrinks by at least 3 %
MAX_FRAME = 2 * CHUNK           # sanity limit for a stored zstd chunk

_FIXED = struct.Struct("<4sBBBBQqIH")   # 30 bytes
_CRC = struct.Struct("<I")
_LEN = struct.Struct("<I")
_TRAILER = struct.Struct("<4s32sQ")     # 44 bytes (+ CRC)
TRAILER_SIZE = _TRAILER.size + _CRC.size

# Formats that are already compressed – zstd would only cost time
RAW_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mxf", ".mkv", ".avi", ".mts", ".m2ts", ".webm", ".wmv", ".flv",
    ".braw", ".r3d", ".crm", ".insv", ".lrv", ".thm",
    ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".flac",
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".dng", ".cr2", ".cr3", ".nef", ".arw",
    ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".zst", ".dmg",
}


class ContainerError(ValueError):
    """The stream does not contain a valid record at this position."""


class SourceChangedError(OSError):
    """A source file changed size while it was being sent."""


@dataclass
class RecordHeader:
    type: int
    flags: int
    size: int
    mtime: int
    mode: int
    path: str
    offset: int = 0       # stream position of the header

    @property
    def compressed(self) -> bool:
        return bool(self.flags & F_ZSTD)

    def pack(self) -> bytes:
        raw = self.path.encode("utf-8")
        fixed = _FIXED.pack(REC_MAGIC, REC_VERSION, self.type, self.flags, 0,
                            self.size, self.mtime, self.mode, len(raw))
        body = fixed + raw
        return body + _CRC.pack(zlib.crc32(body))


def _pack_trailer(sha: bytes, stored: int) -> bytes:
    body = _TRAILER.pack(TRAILER_MAGIC, sha, stored)
    return body + _CRC.pack(zlib.crc32(body))


# ---------------------------------------------------------------- writing

def should_compress(path: Path, size: int, level: int) -> bool:
    """Decide per file: known compressed formats stay raw; otherwise test a sample."""
    if level <= 0 or size == 0 or path.suffix.lower() in RAW_EXTENSIONS:
        return False
    with open(path, "rb") as f:
        sample = f.read(min(size, SAMPLE))
    packed = zstandard.ZstdCompressor(level=level).compress(sample)
    return len(packed) <= len(sample) * (1 - MIN_SAVING)


class RecordWriter:
    """Writes records into a sink that has `write()` and a `total` byte counter."""

    def __init__(self, sink, level: int, on_bytes: Callable[[int], None], cancel=None):
        self.sink = sink
        self.level = level
        self.on_bytes = on_bytes
        self.cancel = cancel
        self._cctx = zstandard.ZstdCompressor(level=level, write_checksum=True) if level > 0 else None

    def write_dir(self, rel: str, mtime: int) -> int:
        offset = self.sink.total
        self.sink.write(RecordHeader(T_DIR, 0, 0, mtime, 0o755, rel).pack())
        return offset

    def write_end(self) -> int:
        offset = self.sink.total
        self.sink.write(RecordHeader(T_END, 0, 0, 0, 0, "").pack())
        return offset

    def write_file(self, path: Path, rel: str, size: int, mtime: int) -> dict:
        """Write one file record. Returns its index entry (offset, length, sha256, compression)."""
        offset = self.sink.total
        zstd = self._cctx is not None and should_compress(path, size, self.level)
        self.sink.write(RecordHeader(T_FILE, F_ZSTD if zstd else 0, size, mtime, 0o644, rel).pack())
        data_start = self.sink.total
        h = hashlib.sha256()
        remaining = size
        with open(path, "rb") as f:
            while remaining:
                check_cancel(self.cancel)
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    raise SourceChangedError(f"\"{rel}\" got shorter while sending")
                remaining -= len(chunk)
                h.update(chunk)
                if zstd:
                    frame = self._cctx.compress(chunk)
                    self.sink.write(_LEN.pack(len(frame)))
                    self.sink.write(frame)
                else:
                    self.sink.write(chunk)
                self.on_bytes(len(chunk))
            if f.read(1):
                raise SourceChangedError(f"\"{rel}\" got larger while sending")
        if zstd:
            self.sink.write(_LEN.pack(0))
        stored = self.sink.total - data_start
        self.sink.write(_pack_trailer(h.digest(), stored))
        return {"offset": offset, "length": self.sink.total - offset,
                "sha256": h.hexdigest(), "compression": "zstd" if zstd else "raw"}


# ---------------------------------------------------------------- reading

def _parse_header_at(buf: bytes, i: int):
    """Try to parse a record header at buf[i:]. Returns its length, None if invalid,
    or -1 if more bytes are needed to decide."""
    if len(buf) - i < _FIXED.size:
        return -1
    magic, version, rtype, _flags, _res, _size, _mtime, _mode, plen = _FIXED.unpack_from(buf, i)
    if magic != REC_MAGIC or version != REC_VERSION or rtype not in (T_FILE, T_DIR, T_END):
        return None
    end = i + _FIXED.size + plen + _CRC.size
    if len(buf) < end:
        return -1
    (crc,) = _CRC.unpack_from(buf, end - _CRC.size)
    return end - i if crc == zlib.crc32(buf[i:end - _CRC.size]) else None


class RecordReader:
    """Parses records from a stream that has `read(n)`, `pos` and `skip_to(pos)`.

    Bytes read ahead while searching for the next record (`resync`) are kept in
    a small push-back buffer, so `pos` always refers to the next unparsed byte.
    """

    def __init__(self, stream, cancel=None):
        self.stream = stream
        self.cancel = cancel
        self._dctx = zstandard.ZstdDecompressor()
        self._pending = b""

    @property
    def pos(self) -> int:
        return self.stream.pos - len(self._pending)

    def _read_exact(self, n: int) -> bytes:
        if self._pending:
            data, self._pending = self._pending[:n], self._pending[n:]
            if len(data) < n:
                data += self.stream.read(n - len(data))
        else:
            data = self.stream.read(n)
        if len(data) != n:
            raise ContainerError("Datenstrom endet unerwartet")
        return data

    def skip_to(self, target: int) -> None:
        d = target - self.pos
        if d < 0:
            raise ValueError("can only skip forward")
        if d <= len(self._pending):
            self._pending = self._pending[d:]
        else:
            rest = d - len(self._pending)
            self._pending = b""
            self.stream.skip_to(self.stream.pos + rest)

    def reset(self) -> None:
        """Forget read-ahead bytes (after the stream was repositioned from outside)."""
        self._pending = b""

    def resync(self, start: int) -> bool:
        """Find the next valid record header at or after `start` (emergency recovery).

        Searches for the record magic and accepts a candidate only if its header
        CRC matches. Returns False at the end of the stream.
        """
        self._pending = b""
        if start > self.stream.pos:
            self.stream.skip_to(start)
        buf, scan_from, eof = b"", 0, False
        while True:
            check_cancel(self.cancel)
            i = buf.find(REC_MAGIC, scan_from)
            if i != -1:
                r = _parse_header_at(buf, i)
                if r is None:
                    scan_from = i + 1
                    continue
                if r > 0 or eof:
                    if r > 0:
                        self._pending = buf[i:]
                        return True
                    scan_from = i + 1
                    continue
            elif eof:
                return False
            else:
                # keep a tail so a magic split across reads is still found
                keep = max(scan_from, len(buf) - (len(REC_MAGIC) - 1))
                buf, scan_from = buf[keep:], 0
            chunk = self.stream.read(CHUNK)
            eof = not chunk
            buf += chunk

    def next(self) -> RecordHeader:
        offset = self.pos
        fixed = self._read_exact(_FIXED.size)
        magic, version, rtype, flags, _res, size, mtime, mode, plen = _FIXED.unpack(fixed)
        if magic != REC_MAGIC:
            raise ContainerError(f"no record at position {offset}")
        if version != REC_VERSION or rtype not in (T_FILE, T_DIR, T_END):
            raise ContainerError(f"unbekannter Record-Typ an Position {offset}")
        raw = self._read_exact(plen)
        (crc,) = _CRC.unpack(self._read_exact(_CRC.size))
        if crc != zlib.crc32(fixed + raw):
            raise ContainerError(f"record header at position {offset} is damaged")
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContainerError(f"invalid path at position {offset}") from exc
        return RecordHeader(rtype, flags, size, mtime, mode, path, offset)

    def read_data(self, rec: RecordHeader, write: Callable[[bytes], None]) -> str:
        """Stream the original file data to `write`, verify the trailer, return the SHA-256 hex."""
        h = hashlib.sha256()
        start = self.pos
        produced = 0
        if rec.compressed:
            while True:
                check_cancel(self.cancel)
                (n,) = _LEN.unpack(self._read_exact(_LEN.size))
                if n == 0:
                    break
                if n > MAX_FRAME:
                    raise ContainerError(f"\"{rec.path}\": invalid block length")
                try:
                    chunk = self._dctx.decompress(self._read_exact(n), max_output_size=CHUNK)
                except zstandard.ZstdError as exc:
                    raise ContainerError(f"\"{rec.path}\": data damaged ({exc})") from exc
                produced += len(chunk)
                if produced > rec.size:
                    raise ContainerError(f"\"{rec.path}\": more data than declared")
                h.update(chunk)
                write(chunk)
        else:
            remaining = rec.size
            while remaining:
                check_cancel(self.cancel)
                chunk = self._read_exact(min(CHUNK, remaining))
                remaining -= len(chunk)
                produced += len(chunk)
                h.update(chunk)
                write(chunk)
        stored = self.pos - start
        body = self._read_exact(_TRAILER.size)
        (crc,) = _CRC.unpack(self._read_exact(_CRC.size))
        magic, sha, stored_expected = _TRAILER.unpack(body)
        if magic != TRAILER_MAGIC or crc != zlib.crc32(body):
            raise ContainerError(f"\"{rec.path}\": trailer damaged")
        if produced != rec.size or stored != stored_expected:
            raise ContainerError(f"\"{rec.path}\": length does not match")
        if h.digest() != sha:
            raise ContainerError(f"\"{rec.path}\": wrong checksum")
        return sha.hex()

    def skip_data(self, rec: RecordHeader, length: Optional[int]) -> None:
        """Skip the rest of a file record; jumps directly if the record length is known."""
        if length is not None:
            self.skip_to(rec.offset + length)
        else:
            self.read_data(rec, lambda _b: None)
