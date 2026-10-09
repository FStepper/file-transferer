"""Sender: folder → record container (v2) → stripes → packets in the Drive folder.

The generated stream is deterministic (sorted files, fixed record headers,
deterministic per-file compression decisions, single-threaded zstd).
Restarting an interrupted transfer therefore produces exactly the same
packets: identical packets already in Drive are skipped, damaged ones are
replaced. Format v1 (one zstd-compressed tar stream) can still be written via
`SendOptions(format_version=1)` to test backwards compatibility.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import zstandard

from vidtransfer import __version__
from vidtransfer.erasure import encode_stripe
from vidtransfer.container import RecordWriter
from vidtransfer.manifest import (
    FORMAT_VERSION, STATUS_COMPLETE, STATUS_IN_PROGRESS, format_version, read_manifest, write_manifest,
)
from vidtransfer.packet import PacketHeader, read_header, verify_packet
from vidtransfer.util import (
    Cancelled, ProgressFn, atomic_write_bytes, check_cancel, human_bytes, null_progress, replace,
)

log = logging.getLogger(__name__)

MIB = 1 << 20
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r", ".localized"}

README_NAME = "README.txt"
README_TEXT = """\
This folder was created by VidTransfer.

It contains the folder "{name}", split into fault-tolerant packets (*.vtp).
To restore it, drag this folder onto the VidTransfer icon
(or run: vidtransfer receive "<this folder>" "<destination>").

Please do not rename or delete anything in this folder until the transfer
has been received.
"""


@dataclass
class SendOptions:
    k: int = 10                  # data packets per stripe
    m: int = 3                   # parity packets per stripe
    block_size: int = 32 * MIB   # payload size of one packet
    zstd_level: int = 3          # 0 = no compression
    format_version: int = FORMAT_VERSION   # 2 = record container; 1 = legacy tar stream

    def validate(self) -> None:
        if not (1 <= self.k <= 99 and 0 <= self.m <= 99 and self.k + self.m <= 100):
            raise ValueError("k/m außerhalb des erlaubten Bereichs (k+m ≤ 100)")
        if self.block_size < 4096:
            raise ValueError("Blockgröße zu klein")
        if self.format_version not in (1, 2):
            raise ValueError(f"unbekanntes Format {self.format_version}")

    def params(self) -> dict:
        if self.format_version == 1:
            compression = "zstd" if self.zstd_level else "none"
        else:
            compression = "per-file" if self.zstd_level else "none"
        return {
            "k": self.k, "m": self.m, "block_size": self.block_size,
            "compression": compression, "zstd_level": self.zstd_level,
        }


@dataclass
class SourceEntry:
    rel: str          # POSIX path relative to the source folder
    path: Path
    is_dir: bool
    size: int
    mtime: int


@dataclass
class SendResult:
    target: Path
    transfer_id: str
    stripes: int
    stream_bytes: int
    source_bytes: int
    packets_written: int
    packets_skipped: int
    resumed: bool
    files: list = field(default_factory=list)


# ---------------------------------------------------------------- source

def _ignored(name: str) -> bool:
    return name in IGNORED_NAMES or name.startswith("._")


def _rel(p: Path, src: Path) -> str:
    # NFC: macOS file systems hand out decomposed (NFD) names; normalising makes
    # names, hashes and comparisons identical on every platform.
    return unicodedata.normalize("NFC", p.relative_to(src).as_posix())


def scan_source(src: Path) -> list[SourceEntry]:
    entries: list[SourceEntry] = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if not _ignored(d))
        base = Path(dirpath)
        for d in dirnames:
            p = base / d
            if p.is_symlink():
                continue
            st = p.stat()
            entries.append(SourceEntry(_rel(p, src), p, True, 0, int(st.st_mtime)))
        for fn in sorted(filenames):
            p = base / fn
            if _ignored(fn) or p.is_symlink() or not p.is_file():
                continue
            st = p.stat()
            entries.append(SourceEntry(_rel(p, src), p, False, st.st_size, int(st.st_mtime)))
    entries.sort(key=lambda e: e.rel)
    return entries


def source_signature(entries: list[SourceEntry]) -> str:
    sig = [(e.rel, e.is_dir, e.size, e.mtime) for e in entries]
    return hashlib.sha256(json.dumps(sig, ensure_ascii=False).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- target

def _choose_target(drive_root: Path, name: str, signature: str, opts: SendOptions):
    """Return (target folder, transfer ID, previous manifest | None)."""
    i = 1
    while True:
        cand = drive_root / (name if i == 1 else f"{name} ({i})")
        i += 1
        if not cand.exists():
            cand.mkdir()
            return cand, uuid.uuid4().bytes, None
        if not cand.is_dir():
            continue
        man = read_manifest(cand)
        if (man and man.get("source_signature") == signature and man.get("params") == opts.params()
                and format_version(man) == opts.format_version):
            return cand, bytes.fromhex(man["transfer_id"]), man
        if not any(not _ignored(p.name) for p in cand.iterdir()):
            return cand, uuid.uuid4().bytes, None


# ---------------------------------------------------------------- stream

class _HashingReader:
    """Reads a source file for tarfile while computing SHA-256 and reporting progress."""

    def __init__(self, f, on_bytes, cancel):
        self._f = f
        self._on_bytes = on_bytes
        self._cancel = cancel
        self.sha = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        check_cancel(self._cancel)
        data = self._f.read(n)
        self.sha.update(data)
        self._on_bytes(len(data))
        return data


class _StripeSink:
    """Receives the compressed stream and cuts it into stripes."""

    def __init__(self, staging: Path, stripe_bytes: int, on_stripe):
        self._path = staging / "stripe.bin"
        self._stripe_bytes = stripe_bytes
        self._on_stripe = on_stripe
        self._f = None
        self._fill = 0
        self.stripe = 0
        self.total = 0
        self.stripe_lens: list[int] = []

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        mv = memoryview(data)
        written = len(mv)
        while mv:
            if self._f is None:
                self._f = open(self._path, "wb")
                self._fill = 0
            n = min(len(mv), self._stripe_bytes - self._fill)
            self._f.write(mv[:n])
            self._fill += n
            mv = mv[n:]
            if self._fill == self._stripe_bytes:
                self._finish_stripe()
        self.total += written
        return written

    def flush(self) -> None:
        pass

    def _finish_stripe(self) -> None:
        self._f.close()
        self._f = None
        self.stripe_lens.append(self._fill)
        self._on_stripe(self.stripe, self._path, self._fill)
        self.stripe += 1

    def close(self) -> None:
        if self._f is not None:
            if self._fill:
                self._finish_stripe()
            else:
                self._f.close()
                self._f = None


def _publish(packet: Path, hdr: PacketHeader, target: Path) -> bool:
    """Copy a packet into Drive unless an identical, intact one is already there."""
    dest = target / packet.name
    if read_header(dest) == hdr and verify_packet(dest) == hdr:
        packet.unlink()
        return False
    tmp = target / f".{packet.name}.tmp"
    shutil.copyfile(packet, tmp)
    replace(tmp, dest)
    packet.unlink()
    return True


# ---------------------------------------------------------------- stream writers

def _write_tar_stream(sink: "_StripeSink", entries: list[SourceEntry], opts: SendOptions,
                      manifest: dict, on_bytes, cancel) -> None:
    """Format v1: one zstd-compressed tar stream (kept for compatibility tests)."""
    if opts.zstd_level:
        cctx = zstandard.ZstdCompressor(level=opts.zstd_level, write_checksum=True, threads=0)
        stream = cctx.stream_writer(sink, closefd=False)
    else:
        stream = sink
    file_hashes: dict[str, str] = {}
    with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for e in entries:
            check_cancel(cancel)
            ti = tarfile.TarInfo(e.rel)
            ti.mtime = e.mtime
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if e.is_dir:
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tar.addfile(ti)
                continue
            ti.size = e.size
            ti.mode = 0o644
            with open(e.path, "rb") as f:
                reader = _HashingReader(f, on_bytes, cancel)
                tar.addfile(ti, reader)
            file_hashes[e.rel] = reader.sha.hexdigest()
    if stream is not sink:
        stream.close()
    for f in manifest["files"]:
        f["sha256"] = file_hashes[f["path"]]


def _write_records(sink: "_StripeSink", entries: list[SourceEntry], opts: SendOptions,
                   manifest: dict, on_bytes, cancel) -> None:
    """Format v2: one record per file/directory; fills the manifest index as it goes."""
    writer = RecordWriter(sink, opts.zstd_level, on_bytes, cancel)
    files = {f["path"]: f for f in manifest["files"]}
    dirs = {d["path"]: d for d in manifest["dirs"]}
    for e in entries:
        check_cancel(cancel)
        if e.is_dir:
            dirs[e.rel]["offset"] = writer.write_dir(e.rel, e.mtime)
        else:
            files[e.rel].update(writer.write_file(e.path, e.rel, e.size, e.mtime))
    writer.write_end()


# ---------------------------------------------------------------- entry points

class DriveFolderMissingError(FileNotFoundError):
    """The (Google Drive) target folder does not exist – e.g. Drive is not running."""


class SourceMissingError(FileNotFoundError):
    """A folder or file to send no longer exists (moved, deleted, drive unplugged)."""


def _check_drive_root(drive_root: Path) -> Path:
    drive_root = Path(drive_root)
    if not drive_root.is_dir():
        raise DriveFolderMissingError(f"Cloud-Ordner {drive_root} nicht gefunden")
    return drive_root


def send_folder(
    src: Path,
    drive_root: Path,
    opts: Optional[SendOptions] = None,
    progress: ProgressFn = null_progress,
    cancel: Optional[threading.Event] = None,
    sender: Optional[str] = None,
) -> SendResult:
    """Send one folder; the delivery gets the folder's name."""
    src = Path(src).resolve()
    if not src.is_dir():
        raise SourceMissingError(f"Ordner {src} nicht gefunden")
    drive_root = _check_drive_root(drive_root)
    if drive_root.resolve() == src or src in drive_root.resolve().parents:
        raise ValueError("Der Cloud-Ordner darf nicht im Quellordner liegen")
    progress(0, 0, "Ordner wird eingelesen …")
    return _send(src.name, scan_source(src), drive_root, opts, progress, cancel, sender, str(src))


def send_files(
    files: list[Path],
    name: str,
    drive_root: Path,
    opts: Optional[SendOptions] = None,
    progress: ProgressFn = null_progress,
    cancel: Optional[threading.Event] = None,
    sender: Optional[str] = None,
) -> SendResult:
    """Send loose files as one delivery called `name` (files end up side by side)."""
    drive_root = _check_drive_root(drive_root)
    entries: list[SourceEntry] = []
    taken: set[str] = set()
    for f in files:
        f = Path(f)
        if not f.is_file():
            raise SourceMissingError(f"Datei {f} nicht gefunden")
        rel = unicodedata.normalize("NFC", f.name)
        n, stem, suffix = 2, Path(rel).stem, Path(rel).suffix
        while rel.casefold() in taken:               # same name from different folders
            rel = f"{stem} ({n}){suffix}"
            n += 1
        taken.add(rel.casefold())
        st = f.stat()
        entries.append(SourceEntry(rel, f, False, st.st_size, int(st.st_mtime)))
    if not entries:
        raise ValueError("keine Dateien zum Senden")
    entries.sort(key=lambda e: e.rel)
    return _send(name, entries, drive_root, opts, progress, cancel, sender, f"{len(entries)} files")


def _send(name: str, entries: list[SourceEntry], drive_root: Path, opts: Optional[SendOptions],
          progress: ProgressFn, cancel: Optional[threading.Event], sender: Optional[str],
          what: str) -> SendResult:
    opts = opts or SendOptions()
    opts.validate()
    total_bytes = sum(e.size for e in entries)
    signature = source_signature(entries)

    target, tid, old_manifest = _choose_target(drive_root, name, signature, opts)
    resumed = old_manifest is not None
    log.info("Sending %s -> %s (%s, %s)", what, target, human_bytes(total_bytes),
             "resumed" if resumed else "new")

    v2 = opts.format_version == 2
    manifest = {
        "format_version": opts.format_version,
        "transfer_id": tid.hex(),
        "name": name,
        "sender": sender,
        "created_at": (old_manifest or {}).get("created_at")
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app_version": __version__,
        "status": STATUS_IN_PROGRESS,
        "params": opts.params(),
        "source_signature": signature,
        "source_bytes": total_bytes,
        "files": [{"path": e.rel, "size": e.size, **({"mtime": e.mtime} if v2 else {})}
                  for e in entries if not e.is_dir],
        "dirs": [{"path": e.rel, "mtime": e.mtime} if v2 else e.rel for e in entries if e.is_dir],
    }
    atomic_write_bytes(target / README_NAME, README_TEXT.format(name=name).encode("utf-8"))
    # Never downgrade a completed manifest while re-sending (a receiver may be using it)
    keep_complete = bool(old_manifest and old_manifest.get("status") == STATUS_COMPLETE)
    if not keep_complete:
        write_manifest(target, manifest)

    staging = Path(tempfile.mkdtemp(prefix="vidtransfer-send-"))
    out_dir = staging / "out"
    out_dir.mkdir()
    done = 0
    written = skipped = 0
    packet_hashes: dict[str, str] = {}
    last_report = 0.0

    def on_bytes(n: int) -> None:
        nonlocal done, last_report
        done += n
        now = time.monotonic()
        if now - last_report > 0.25:
            last_report = now
            progress(done, total_bytes, f"Packe … ({human_bytes(done)} / {human_bytes(total_bytes)})")

    def on_stripe(stripe: int, path: Path, length: int) -> None:
        nonlocal written, skipped
        progress(done, total_bytes, f"Stripe {stripe + 1}: Parität wird berechnet …")
        packets = encode_stripe(path, length, opts.k, opts.m, stripe, tid, out_dir, cancel)
        for p, hdr in packets:
            check_cancel(cancel)
            packet_hashes[p.name] = hdr.sha256.hex()
            if _publish(p, hdr, target):
                written += 1
            else:
                skipped += 1
        log.info("Stripe %d done (%s)", stripe, human_bytes(length))
        if v2 and not keep_complete:
            # Incremental manifest: the receiver can already map finished files
            manifest["packets"] = packet_hashes
            write_manifest(target, manifest)

    try:
        sink = _StripeSink(staging, opts.k * opts.block_size, on_stripe)
        writer = _write_records if v2 else _write_tar_stream
        writer(sink, entries, opts, manifest, on_bytes, cancel)
        sink.close()

        manifest.update(
            status=STATUS_COMPLETE,
            completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            stripes=sink.stripe,
            stripe_lens=sink.stripe_lens,
            stream_bytes=sink.total,
            packets=packet_hashes,
        )
        write_manifest(target, manifest)
    except Cancelled:
        log.info("Sending cancelled")
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    msg = (f"Fertig: {sink.stripe} Stripes, {written} Pakete geschrieben"
           + (f", {skipped} bereits vorhanden" if skipped else ""))
    progress(total_bytes, total_bytes, msg)
    log.info("Send finished: %d stripes, %d packets written, %d skipped", sink.stripe, written, skipped)
    return SendResult(target, tid.hex(), sink.stripe, sink.total, total_bytes,
                      written, skipped, resumed, manifest["files"])
