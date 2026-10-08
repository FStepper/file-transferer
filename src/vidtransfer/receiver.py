"""Empfänger: Pakete prüfen → ggf. rekonstruieren → zstd → tar → Dateien.

Die Stripes werden der Reihe nach verarbeitet. Fehlt für einen Stripe noch
etwas (Drive synchronisiert noch, oder der Sender läuft noch), wird gewartet,
bis mindestens k gültige Pakete vorhanden sind. Der Empfang kann also schon
starten, während der Upload noch läuft.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

import zstandard

from vidtransfer.erasure import decode_stripe
from vidtransfer.manifest import STATUS_COMPLETE, read_manifest
from vidtransfer.packet import (
    HEADER_SIZE, PacketHeader, data_len, packet_name, parse_packet_name, read_header, verify_packet,
)
from vidtransfer.util import (
    Cancelled, ProgressFn, check_cancel, human_bytes, null_progress, sleep_cancellable, unique_path,
)

log = logging.getLogger(__name__)

POLL_SECONDS = 20
COPY_CHUNK = 1 << 20


class TransferError(Exception):
    pass


@dataclass
class ReceiveResult:
    dest: Path
    files: int
    bytes: int
    repaired_stripes: int


@dataclass
class StripeStatus:
    stripe: int
    present: int       # Pakete mit plausiblem Header und passender Größe
    needed: int        # k
    total: int         # k+m

    @property
    def recoverable(self) -> bool:
        return self.present >= self.needed


# ---------------------------------------------------------------- Status (schnell, ohne Hashen)

def transfer_status(folder: Path) -> tuple[Optional[dict], list[StripeStatus]]:
    """Schneller Überblick anhand von Headern und Dateigrößen (liest keine Nutzdaten)."""
    man = read_manifest(folder)
    if not man:
        return None, []
    p = man["params"]
    k, m = p["k"], p["m"]
    tid = bytes.fromhex(man["transfer_id"])
    complete = man.get("status") == STATUS_COMPLETE
    stripes = man["stripes"] if complete else _highest_stripe(folder) + 1
    result = []
    for s in range(stripes):
        present = 0
        for i in range(k + m):
            path = folder / packet_name(s, i)
            hdr = read_header(path)
            if hdr and hdr.transfer_id == tid and hdr.stripe == s and hdr.index == i:
                try:
                    if path.stat().st_size == HEADER_SIZE + hdr.payload_len:
                        present += 1
                except OSError:
                    pass
        result.append(StripeStatus(s, present, k, k + m))
    return man, result


def _highest_stripe(folder: Path) -> int:
    best = -1
    try:
        for p in folder.iterdir():
            parsed = parse_packet_name(p.name)
            if parsed:
                best = max(best, parsed[0])
    except OSError:
        pass
    return best


# ---------------------------------------------------------------- Stripe-Datenquelle

class _StripeStream:
    """Liefert den (komprimierten) Datenstrom Stripe für Stripe als lesbares Objekt."""

    def __init__(self, folder: Path, staging: Path, progress: ProgressFn, cancel, wait: bool):
        self.folder = folder
        self.staging = staging
        self.progress = progress
        self.cancel = cancel
        self.wait = wait
        self.manifest = read_manifest(folder)
        p = self.manifest["params"]
        self.k, self.m = p["k"], p["m"]
        self.tid = bytes.fromhex(self.manifest["transfer_id"])
        self.stripe = 0
        self.consumed = 0
        self.repaired = 0
        self._segments: list[tuple[Path, int, int]] = []  # (Datei, Offset, Länge)
        self._fh = None
        self._remaining = 0
        self._eof = False

    # -- Fortschritt
    def _total(self) -> int:
        man = self.manifest
        return man.get("stream_bytes") or man.get("source_bytes") or 0

    def _report(self, msg: str) -> None:
        self.progress(self.consumed, self._total(), msg)

    def _refresh_manifest(self) -> None:
        if self.manifest.get("status") != STATUS_COMPLETE:
            new = read_manifest(self.folder)
            if new and new["transfer_id"] == self.manifest["transfer_id"]:
                self.manifest = new

    def _complete(self) -> bool:
        return self.manifest.get("status") == STATUS_COMPLETE

    # -- Paketprüfung
    def _check(self, s: int, i: int) -> Optional[PacketHeader]:
        path = self.folder / packet_name(s, i)
        if not path.exists():
            return None
        hdr = verify_packet(path, self.cancel)
        if not hdr or hdr.transfer_id != self.tid or hdr.stripe != s or hdr.index != i \
                or hdr.k != self.k or hdr.m != self.m:
            return None
        expected = (self.manifest.get("packets") or {}).get(path.name)
        if expected and expected != hdr.sha256.hex():
            return None
        lens = self.manifest.get("stripe_lens")
        if lens and s < len(lens) and lens[s] != hdr.stripe_len:
            return None
        return hdr

    def _load_stripe(self, s: int) -> bool:
        """Bereitet Stripe `s` zum Lesen vor. False = Strom ist zu Ende."""
        while True:
            check_cancel(self.cancel)
            self._refresh_manifest()
            if self._complete() and s >= self.manifest["stripes"]:
                return False

            valid: dict[int, PacketHeader] = {}
            for i in range(self.k):
                self._report(f"Stripe {s + 1}: prüfe Paket {i + 1}/{self.k + self.m} …")
                hdr = self._check(s, i)
                if hdr:
                    valid[i] = hdr
            if len(valid) < self.k:
                for i in range(self.k, self.k + self.m):
                    if len(valid) >= self.k:
                        break
                    self._report(f"Stripe {s + 1}: prüfe Paritätspaket {i - self.k + 1}/{self.m} …")
                    hdr = self._check(s, i)
                    if hdr:
                        valid[i] = hdr

            stripe_lens = {h.stripe_len for h in valid.values()}
            if len(valid) >= self.k and len(stripe_lens) == 1:
                stripe_len = stripe_lens.pop()
                if all(i in valid for i in range(self.k)):
                    self._segments = [
                        (self.folder / packet_name(s, i), HEADER_SIZE, data_len(stripe_len, self.k, i))
                        for i in range(self.k)
                    ]
                else:
                    missing = [i for i in range(self.k) if i not in valid]
                    log.info("Stripe %d: Datenpakete %s fehlen/defekt → Rekonstruktion", s, missing)
                    self._report(f"Stripe {s + 1}: rekonstruiere {len(missing)} Paket(e) …")
                    out = self.staging / "stripe.bin"
                    decode_stripe({i: self.folder / packet_name(s, i) for i in valid},
                                  stripe_len, self.k, self.m, out, self.cancel)
                    self._segments = [(out, 0, stripe_len)]
                    self.repaired += 1
                return True

            if not self.wait:
                raise TransferError(
                    f"Stripe {s + 1}: nur {len(valid)} von mindestens {self.k} gültigen Paketen vorhanden")
            if self._complete():
                hint = ("Upload abgeschlossen, aber Pakete fehlen/sind defekt – Drive synchronisiert "
                        "evtl. noch. Notfalls auf dem Sender denselben Ordner erneut senden (repariert).")
            else:
                hint = "Sender lädt noch hoch"
            self._report(f"Warte auf Stripe {s + 1} ({len(valid)}/{self.k} Pakete) – {hint}")
            log.info("Warte auf Stripe %d (%d/%d gültig)", s, len(valid), self.k)
            sleep_cancellable(POLL_SECONDS, self.cancel)

    # -- Datei-ähnliche Schnittstelle für zstd/tarfile
    def _next_segment(self) -> bool:
        if self._fh:
            self._fh.close()
            self._fh = None
        while not self._segments:
            if self._eof or not self._load_stripe(self.stripe):
                self._eof = True
                return False
            self.stripe += 1
        path, off, length = self._segments.pop(0)
        self._fh = open(path, "rb")
        self._fh.seek(off)
        self._remaining = length
        return True

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = 1 << 30
        out = bytearray()
        while len(out) < size:
            if self._remaining == 0 and not self._next_segment():
                break
            chunk = self._fh.read(min(size - len(out), self._remaining))
            if not chunk:
                raise TransferError("Paket wurde während des Lesens verändert")
            self._remaining -= len(chunk)
            out += chunk
        self.consumed += len(out)
        return bytes(out)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------- Entpacken

def _safe_target(root: Path, name: str) -> Optional[Path]:
    pp = PurePosixPath(name)
    if pp.is_absolute() or any(part in ("..", "") for part in pp.parts) or ":" in pp.parts[0]:
        return None
    return root.joinpath(*pp.parts)


def receive_folder(
    folder: Path,
    dest_parent: Path,
    progress: ProgressFn = null_progress,
    cancel: Optional[threading.Event] = None,
    wait: bool = True,
) -> ReceiveResult:
    folder = Path(folder)
    dest_parent = Path(dest_parent)

    man = read_manifest(folder)
    while not man:
        if not wait:
            raise TransferError(f"Kein gültiges Manifest in {folder}")
        progress(0, 0, "Warte auf Manifest …")
        sleep_cancellable(POLL_SECONDS, cancel)
        man = read_manifest(folder)

    name = man["name"]
    dest_parent.mkdir(parents=True, exist_ok=True)
    final = unique_path(dest_parent / name)
    partial = dest_parent / f"{final.name} (wird empfangen)"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()
    staging = Path(tempfile.mkdtemp(prefix="vidtransfer-recv-"))
    log.info("Empfange %s → %s", folder, final)

    extracted: dict[str, str] = {}
    total_bytes = 0
    stream = _StripeStream(folder, staging, progress, cancel, wait)
    try:
        if man["params"]["compression"] == "zstd":
            source = zstandard.ZstdDecompressor().stream_reader(stream, closefd=False)
        else:
            source = stream
        mtimes: list[tuple[Path, int]] = []
        with tarfile.open(fileobj=source, mode="r|") as tar:
            for member in tar:
                check_cancel(cancel)
                target = _safe_target(partial, member.name)
                if target is None:
                    log.warning("Unsicherer Pfad übersprungen: %r", member.name)
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    mtimes.append((target, member.mtime))
                    continue
                if not member.isfile():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                src = tar.extractfile(member)
                with open(target, "wb") as out:
                    while True:
                        check_cancel(cancel)
                        chunk = src.read(COPY_CHUNK)
                        if not chunk:
                            break
                        h.update(chunk)
                        out.write(chunk)
                os.utime(target, (member.mtime, member.mtime))
                extracted[member.name] = h.hexdigest()
                total_bytes += member.size
                progress(stream.consumed, stream._total(), f"Entpackt: {member.name}")
        if source is not stream:
            source.close()
        for path, mtime in reversed(mtimes):
            os.utime(path, (mtime, mtime))

        # Auf das abschließende Manifest warten (enthält die Datei-Hashes)
        man = read_manifest(folder)
        while not man or man.get("status") != STATUS_COMPLETE:
            if not wait:
                raise TransferError("Manifest ist noch nicht abgeschlossen")
            progress(stream.consumed, stream._total(), "Warte auf abschließendes Manifest …")
            sleep_cancellable(POLL_SECONDS, cancel)
            man = read_manifest(folder)

        progress(stream.consumed, stream._total(), "Prüfe Dateien …")
        errors = []
        for f in man["files"]:
            got = extracted.get(f["path"])
            if got is None:
                errors.append(f"fehlt: {f['path']}")
            elif got != f["sha256"]:
                errors.append(f"Prüfsumme falsch: {f['path']}")
        if errors:
            raise TransferError("Überprüfung fehlgeschlagen:\n" + "\n".join(errors[:20]))
    except BaseException as exc:
        stream.close()
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, Cancelled):
            shutil.rmtree(partial, ignore_errors=True)
        raise
    stream.close()
    shutil.rmtree(staging, ignore_errors=True)

    os.replace(partial, final)
    msg = (f"Fertig: {len(extracted)} Dateien ({human_bytes(total_bytes)}) geprüft"
           + (f", {stream.repaired} Stripe(s) repariert" if stream.repaired else ""))
    progress(1, 1, msg)
    log.info(msg)
    return ReceiveResult(final, len(extracted), total_bytes, stream.repaired)
