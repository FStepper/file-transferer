"""Sender: Ordner → tar → zstd → Stripes → Pakete im Drive-Ordner.

Der erzeugte Datenstrom ist deterministisch (sortierte Dateien, normierte
tar-Header, zstd single-threaded). Wird ein abgebrochener Transfer erneut
gestartet, entstehen deshalb exakt dieselben Pakete. Bereits vorhandene,
identische Pakete im Drive werden übersprungen, defekte ersetzt.
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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import zstandard

from vidtransfer import __version__
from vidtransfer.erasure import encode_stripe
from vidtransfer.manifest import (
    STATUS_COMPLETE, STATUS_IN_PROGRESS, read_manifest, write_manifest,
)
from vidtransfer.packet import PacketHeader, read_header, verify_packet
from vidtransfer.util import (
    Cancelled, ProgressFn, atomic_write_bytes, check_cancel, human_bytes, null_progress,
)

log = logging.getLogger(__name__)

MIB = 1 << 20
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r", ".localized"}

README_TEXT = """\
Dieser Ordner wurde von VidTransfer erzeugt.

Er enthält den Ordner "{name}" in fehlertolerante Pakete (*.vtp) zerlegt.
Zum Wiederherstellen diesen Ordner auf das VidTransfer-Symbol ziehen
(oder: vidtransfer receive "<dieser Ordner>" "<Zielordner>").

Bitte nichts in diesem Ordner umbenennen oder löschen, bevor der Empfang
abgeschlossen ist.
"""


@dataclass
class SendOptions:
    k: int = 10                  # Datenpakete pro Stripe
    m: int = 3                   # Paritätspakete pro Stripe
    block_size: int = 32 * MIB   # Payload-Größe eines Pakets
    zstd_level: int = 3          # 0 = keine Kompression

    def validate(self) -> None:
        if not (1 <= self.k <= 99 and 0 <= self.m <= 99 and self.k + self.m <= 100):
            raise ValueError("k/m außerhalb des erlaubten Bereichs (k+m ≤ 100)")
        if self.block_size < 4096:
            raise ValueError("Blockgröße zu klein")

    def params(self) -> dict:
        return {
            "k": self.k, "m": self.m, "block_size": self.block_size,
            "compression": "zstd" if self.zstd_level else "none",
            "zstd_level": self.zstd_level,
        }


@dataclass
class SourceEntry:
    rel: str          # POSIX-Pfad relativ zum Quellordner
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


# ---------------------------------------------------------------- Quelle

def _ignored(name: str) -> bool:
    return name in IGNORED_NAMES or name.startswith("._")


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
            entries.append(SourceEntry(p.relative_to(src).as_posix(), p, True, 0, int(st.st_mtime)))
        for fn in sorted(filenames):
            p = base / fn
            if _ignored(fn) or p.is_symlink() or not p.is_file():
                continue
            st = p.stat()
            entries.append(SourceEntry(p.relative_to(src).as_posix(), p, False, st.st_size, int(st.st_mtime)))
    entries.sort(key=lambda e: e.rel)
    return entries


def source_signature(entries: list[SourceEntry]) -> str:
    sig = [(e.rel, e.is_dir, e.size, e.mtime) for e in entries]
    return hashlib.sha256(json.dumps(sig, ensure_ascii=False).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- Ziel

def _choose_target(drive_root: Path, name: str, signature: str, opts: SendOptions):
    """Liefert (Zielordner, Transfer-ID, alte Manifestdaten|None)."""
    i = 1
    while True:
        cand = drive_root / (name if i == 1 else f"{name} ({i})")
        i += 1
        if not cand.exists():
            cand.mkdir(parents=True)
            return cand, uuid.uuid4().bytes, None
        if not cand.is_dir():
            continue
        man = read_manifest(cand)
        if man and man.get("source_signature") == signature and man.get("params") == opts.params():
            return cand, bytes.fromhex(man["transfer_id"]), man
        if not any(not _ignored(p.name) for p in cand.iterdir()):
            return cand, uuid.uuid4().bytes, None


# ---------------------------------------------------------------- Stream

class _HashingReader:
    """Liest eine Quelldatei für tarfile, berechnet dabei SHA-256 und meldet Fortschritt."""

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
    """Nimmt den komprimierten Strom entgegen und schneidet ihn in Stripes."""

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
    """Kopiert ein Paket ins Drive, sofern dort nicht schon ein identisches liegt."""
    dest = target / packet.name
    if read_header(dest) == hdr and verify_packet(dest) == hdr:
        packet.unlink()
        return False
    tmp = target / f".{packet.name}.tmp"
    shutil.copyfile(packet, tmp)
    os.replace(tmp, dest)
    packet.unlink()
    return True


# ---------------------------------------------------------------- Hauptfunktion

def send_folder(
    src: Path,
    drive_root: Path,
    opts: Optional[SendOptions] = None,
    progress: ProgressFn = null_progress,
    cancel: Optional[threading.Event] = None,
) -> SendResult:
    opts = opts or SendOptions()
    opts.validate()
    src = Path(src).resolve()
    drive_root = Path(drive_root)
    if not src.is_dir():
        raise NotADirectoryError(f"{src} ist kein Ordner")
    if not drive_root.is_dir():
        raise FileNotFoundError(f"Drive-Ordner {drive_root} nicht gefunden")
    if drive_root.resolve() == src or src in drive_root.resolve().parents:
        raise ValueError("Der Drive-Ordner darf nicht im Quellordner liegen")

    progress(0, 0, "Ordner wird eingelesen …")
    entries = scan_source(src)
    total_bytes = sum(e.size for e in entries)
    signature = source_signature(entries)

    target, tid, old_manifest = _choose_target(drive_root, src.name, signature, opts)
    resumed = old_manifest is not None
    log.info("Sende %s → %s (%s, %s)", src, target, human_bytes(total_bytes),
             "Fortsetzung" if resumed else "neu")

    manifest = {
        "transfer_id": tid.hex(),
        "name": src.name,
        "created_at": (old_manifest or {}).get("created_at")
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app_version": __version__,
        "status": STATUS_IN_PROGRESS,
        "params": opts.params(),
        "source_signature": signature,
        "source_bytes": total_bytes,
        "files": [{"path": e.rel, "size": e.size} for e in entries if not e.is_dir],
        "dirs": [e.rel for e in entries if e.is_dir],
    }
    atomic_write_bytes(target / "LIESMICH.txt", README_TEXT.format(name=src.name).encode("utf-8"))
    if not (old_manifest and old_manifest.get("status") == STATUS_COMPLETE):
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
        log.info("Stripe %d fertig (%s)", stripe, human_bytes(length))

    try:
        sink = _StripeSink(staging, opts.k * opts.block_size, on_stripe)
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
        sink.close()

        manifest.update(
            status=STATUS_COMPLETE,
            completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            stripes=sink.stripe,
            stripe_lens=sink.stripe_lens,
            stream_bytes=sink.total,
            packets=packet_hashes,
        )
        for f in manifest["files"]:
            f["sha256"] = file_hashes[f["path"]]
        write_manifest(target, manifest)
    except Cancelled:
        log.info("Senden abgebrochen")
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    msg = (f"Fertig: {sink.stripe} Stripes, {written} Pakete geschrieben"
           + (f", {skipped} bereits vorhanden" if skipped else ""))
    progress(total_bytes, total_bytes, msg)
    log.info(msg)
    return SendResult(target, tid.hex(), sink.stripe, sink.total, total_bytes,
                      written, skipped, resumed, manifest["files"])
