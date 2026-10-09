"""Receiver: verify packets → reconstruct if needed → zstd → tar → files.

Stripes are processed in order. If something is still missing for a stripe
(Drive is still syncing, or the sender is still uploading), the receiver waits
until at least k valid packets are present, so receiving can start while the
upload is still running.

Unpacking is crash-safe and resumable:
- everything is written into "<name> (receiving)" first and only renamed to
  "<name>" once every file has been verified;
- each file is written as ".<name>.vtpart" and only gets its real name once its
  SHA-256 matches the manifest, so a half-written or corrupt file never appears
  under its real name;
- ".vidtransfer-state.json" records finished files, so an interrupted receive
  continues where it stopped (finished files are not written again).
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

import zstandard

from vidtransfer import __version__
from vidtransfer.erasure import decode_stripe
from vidtransfer.manifest import STATUS_COMPLETE, read_manifest
from vidtransfer.packet import (
    HEADER_SIZE, PacketHeader, data_len, packet_name, parse_packet_name, read_header, verify_packet,
)
from vidtransfer.util import (
    Cancelled, ProgressFn, atomic_write_bytes, check_cancel, free_space, human_bytes,
    human_duration, null_progress, sleep_cancellable, unique_path,
)

log = logging.getLogger(__name__)

POLL_SECONDS = 20
COPY_CHUNK = 1 << 20
SPACE_RESERVE = 1.05

RECEIVING_SUFFIX = " (receiving)"
STATE_NAME = ".vidtransfer-state.json"
WORK_DIR_NAME = ".vidtransfer-work"
PART_SUFFIX = ".vtpart"
REPORT_NAME = "VidTransfer-Report.txt"
_INTERNAL_NAMES = {STATE_NAME, WORK_DIR_NAME, REPORT_NAME}


class TransferError(Exception):
    pass


class InsufficientSpaceError(TransferError):
    pass


@dataclass
class ReceiveResult:
    dest: Path
    files: int
    bytes: int
    repaired_stripes: int
    resumed: bool = False
    skipped_files: int = 0
    report: Optional[Path] = None


@dataclass
class StripeStatus:
    stripe: int
    present: int       # packets with a plausible header and matching size
    needed: int        # k
    total: int         # k+m

    @property
    def recoverable(self) -> bool:
        return self.present >= self.needed


@dataclass
class VerifyResult:
    name: str
    complete: bool                      # sender has finished
    stripes: int
    intact: int                         # stripes with all k+m packets valid
    degraded: list[int] = field(default_factory=list)       # repairable via parity
    unrecoverable: list[int] = field(default_factory=list)  # fewer than k valid packets
    bad_packets: list[str] = field(default_factory=list)
    files_checked: int = 0
    bytes_checked: int = 0
    content_verified: bool = False      # stream decoded and every file hash matched
    content_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.complete and not self.unrecoverable and self.content_verified


# ---------------------------------------------------------------- packet checks

def _check_packet(folder: Path, man: dict, s: int, i: int, cancel=None) -> Optional[PacketHeader]:
    """Fully verify one packet (header, size, SHA-256) against the manifest."""
    p = man["params"]
    path = folder / packet_name(s, i)
    if not path.exists():
        return None
    hdr = verify_packet(path, cancel)
    if (not hdr or hdr.transfer_id != bytes.fromhex(man["transfer_id"]) or hdr.stripe != s
            or hdr.index != i or hdr.k != p["k"] or hdr.m != p["m"]):
        return None
    expected = (man.get("packets") or {}).get(path.name)
    if expected and expected != hdr.sha256.hex():
        return None
    lens = man.get("stripe_lens")
    if lens and s < len(lens) and lens[s] != hdr.stripe_len:
        return None
    return hdr


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


def transfer_status(folder: Path) -> tuple[Optional[dict], list[StripeStatus]]:
    """Quick overview based on headers and file sizes only (reads no payload)."""
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


# ---------------------------------------------------------------- stripe data source

class _StripeStream:
    """Provides the (compressed) data stream stripe by stripe as a readable object."""

    def __init__(self, folder: Path, work: Path, progress: ProgressFn, cancel, wait: bool):
        self.folder = folder
        self.work = work
        self.progress = progress
        self.cancel = cancel
        self.wait = wait
        self.manifest = read_manifest(folder)
        p = self.manifest["params"]
        self.k, self.m = p["k"], p["m"]
        self.stripe = 0
        self.consumed = 0
        self.repaired = 0
        self._segments: list[tuple[Path, int, int]] = []  # (file, offset, length)
        self._fh = None
        self._remaining = 0
        self._eof = False

    # -- progress
    def total(self) -> int:
        man = self.manifest
        return man.get("stream_bytes") or man.get("source_bytes") or 0

    def _report(self, msg: str) -> None:
        self.progress(self.consumed, self.total(), msg)

    def _refresh_manifest(self) -> None:
        if self.manifest.get("status") != STATUS_COMPLETE:
            new = read_manifest(self.folder)
            if new and new["transfer_id"] == self.manifest["transfer_id"]:
                self.manifest = new

    def _complete(self) -> bool:
        return self.manifest.get("status") == STATUS_COMPLETE

    def _load_stripe(self, s: int) -> bool:
        """Prepare stripe `s` for reading. False = end of stream."""
        while True:
            check_cancel(self.cancel)
            self._refresh_manifest()
            if self._complete() and s >= self.manifest["stripes"]:
                return False

            valid: dict[int, PacketHeader] = {}
            for i in range(self.k):
                self._report(f"Stripe {s + 1}: prüfe Paket {i + 1}/{self.k + self.m} …")
                hdr = _check_packet(self.folder, self.manifest, s, i, self.cancel)
                if hdr:
                    valid[i] = hdr
            # Parity packets are only touched when needed (saves downloads)
            for i in range(self.k, self.k + self.m):
                if len(valid) >= self.k:
                    break
                self._report(f"Stripe {s + 1}: prüfe Paritätspaket {i - self.k + 1}/{self.m} …")
                hdr = _check_packet(self.folder, self.manifest, s, i, self.cancel)
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
                    log.info("Stripe %d: data packets %s missing/damaged -> reconstructing", s, missing)
                    self._report(f"Stripe {s + 1}: rekonstruiere {len(missing)} Paket(e) …")
                    out = self.work / "stripe.bin"
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
            log.info("Waiting for stripe %d (%d/%d valid)", s, len(valid), self.k)
            sleep_cancellable(POLL_SECONDS, self.cancel)

    # -- file-like interface for zstd/tarfile
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


def _open_tar(stream: _StripeStream, man: dict):
    """Return (tarfile, decompressor-or-None) reading from the stripe stream."""
    source = None
    if man["params"]["compression"] == "zstd":
        source = zstandard.ZstdDecompressor().stream_reader(stream, closefd=False)
    return tarfile.open(fileobj=source or stream, mode="r|"), source


# ---------------------------------------------------------------- receive state

class _ReceiveState:
    """Progress of a receive, stored inside the "(receiving)" folder."""

    def __init__(self, folder: Path, transfer_id: str, done=None, pending=None):
        self.folder = folder
        self.transfer_id = transfer_id
        # rel path -> {"size", "sha256", "mtime"}
        self.done: dict[str, dict] = done or {}      # verified, under the real name
        self.pending: dict[str, dict] = pending or {}  # written as .vtpart, hash not yet known

    @classmethod
    def load(cls, folder: Path) -> Optional["_ReceiveState"]:
        try:
            raw = json.loads((folder / STATE_NAME).read_text(encoding="utf-8"))
            return cls(folder, raw["transfer_id"], raw.get("done") or {}, raw.get("pending") or {})
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self) -> None:
        raw = {"transfer_id": self.transfer_id, "app_version": __version__,
               "done": self.done, "pending": self.pending}
        atomic_write_bytes(self.folder / STATE_NAME, json.dumps(raw, ensure_ascii=False).encode("utf-8"))


def _prepare_destination(dest_parent: Path, man: dict) -> tuple[Path, Path, _ReceiveState, bool]:
    """Return (final folder, working folder, state, resumed)."""
    name, tid = man["name"], man["transfer_id"]
    i = 1
    while True:
        final = dest_parent / (name if i == 1 else f"{name} ({i})")
        partial = final.with_name(final.name + RECEIVING_SUFFIX)
        i += 1
        if partial.is_dir():
            state = _ReceiveState.load(partial)
            if state and state.transfer_id == tid:
                return final, partial, state, True
            continue
        if partial.exists() or final.exists():
            continue
        partial.mkdir()
        state = _ReceiveState(partial, tid)
        state.save()
        return final, partial, state, False


def _required_space(man: dict, state: _ReceiveState) -> int:
    p = man["params"]
    remaining = sum(f["size"] for f in man["files"]
                    if f["path"] not in state.done and f["path"] not in state.pending)
    return int((remaining + p["k"] * p["block_size"]) * SPACE_RESERVE)


# ---------------------------------------------------------------- unpacking

def _safe_target(root: Path, name: str) -> Optional[Path]:
    pp = PurePosixPath(name)
    if (not pp.parts or pp.is_absolute() or any(part in ("..", "") for part in pp.parts)
            or ":" in pp.parts[0] or pp.parts[0] in _INTERNAL_NAMES):
        return None
    return root.joinpath(*pp.parts)


def _part_path(target: Path) -> Path:
    return target.with_name(f".{target.name}{PART_SUFFIX}")


def _unchanged(path: Path, entry: dict) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_size == entry["size"] and int(st.st_mtime) == entry["mtime"]


def _expected_hashes(man: dict) -> dict[str, str]:
    return {f["path"]: f["sha256"] for f in man.get("files", []) if "sha256" in f}


@dataclass
class _UnpackStats:
    written: int = 0
    skipped: int = 0
    dir_mtimes: list = field(default_factory=list)


def _unpack(stream: _StripeStream, partial: Path, state: _ReceiveState,
            progress: ProgressFn, cancel) -> _UnpackStats:
    stats = _UnpackStats()
    hashes_for, hashes = None, {}
    tar, source = _open_tar(stream, stream.manifest)
    try:
        for member in tar:
            check_cancel(cancel)
            target = _safe_target(partial, member.name)
            if target is None:
                log.warning("Skipping unsafe path: %r", member.name)
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                stats.dir_mtimes.append((target, member.mtime))
                continue
            if not member.isfile():
                continue
            rel = member.name

            # Resume: skip files that are already finished
            if rel in state.done and _unchanged(target, state.done[rel]):
                stats.skipped += 1
                continue
            if rel in state.pending and _unchanged(_part_path(target), state.pending[rel]):
                stats.skipped += 1
                continue
            state.done.pop(rel, None)
            state.pending.pop(rel, None)

            target.parent.mkdir(parents=True, exist_ok=True)
            part = _part_path(target)
            h = hashlib.sha256()
            src = tar.extractfile(member)
            with open(part, "wb") as out:
                while True:
                    check_cancel(cancel)
                    chunk = src.read(COPY_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.utime(part, (member.mtime, member.mtime))
            entry = {"size": member.size, "sha256": h.hexdigest(), "mtime": int(member.mtime)}

            if hashes_for is not stream.manifest:
                hashes_for, hashes = stream.manifest, _expected_hashes(stream.manifest)
            expected = hashes.get(rel)
            if expected is None:
                state.pending[rel] = entry          # verified once the final manifest arrives
            elif expected == entry["sha256"]:
                os.replace(part, target)
                state.done[rel] = entry
            else:
                part.unlink()
                state.save()
                raise TransferError(f"Prüfsumme falsch: {rel} – bitte erneut empfangen")
            state.save()
            stats.written += 1
            progress(stream.consumed, stream.total(), f"Entpackt: {rel}")
    finally:
        tar.close()
        if source is not None:
            source.close()
    return stats


def _finalize(partial: Path, state: _ReceiveState, man: dict) -> None:
    """Check every file against the final manifest and give pending files their real name."""
    errors = []
    for f in man["files"]:
        rel = f["path"]
        target = _safe_target(partial, rel)
        if target is None:
            errors.append(f"unzulässiger Pfad: {rel}")
            continue
        if rel in state.done:
            if state.done[rel]["sha256"] != f["sha256"]:
                state.done.pop(rel)
                errors.append(f"Prüfsumme falsch: {rel}")
            continue
        entry = state.pending.pop(rel, None)
        part = _part_path(target)
        if entry is None:
            errors.append(f"fehlt: {rel}")
        elif entry["sha256"] == f["sha256"] and part.exists():
            os.replace(part, target)
            state.done[rel] = entry
        else:
            part.unlink(missing_ok=True)
            errors.append(f"Prüfsumme falsch: {rel}")
    state.save()
    if errors:
        more = f"\n… und {len(errors) - 20} weitere" if len(errors) > 20 else ""
        raise TransferError("Überprüfung fehlgeschlagen (erneut empfangen schreibt nur die "
                            "betroffenen Dateien neu):\n" + "\n".join(errors[:20]) + more)


def _write_report(dest: Path, man: dict, state: _ReceiveState, repaired: int,
                  skipped: int, started: datetime, seconds: float) -> Path:
    p = man["params"]
    files = sorted(man["files"], key=lambda f: f["path"])
    total = sum(f["size"] for f in files)
    lines = [
        "VidTransfer receive report",
        "==========================",
        "",
        f"Transfer:          {man['name']}",
        f"Transfer ID:       {man['transfer_id']}",
        f"Sent:              {man.get('created_at', '?')}",
        f"Received:          {started.isoformat(timespec='seconds')}",
        f"Duration:          {human_duration(seconds)}",
        f"Files:             {len(files)} ({human_bytes(total)})",
        f"Repaired stripes:  {repaired} of {man.get('stripes', '?')}",
        f"Resumed:           {'yes, ' + str(skipped) + ' file(s) already done' if skipped else 'no'}",
        f"Parameters:        k={p['k']} m={p['m']} packet={human_bytes(p['block_size'])} "
        f"compression={p['compression']}",
        f"App version:       {__version__}",
        "",
        "All files were verified with SHA-256.",
        "",
        f"{'SHA-256':64}  {'Size':>15}  Path",
    ]
    lines += [f"{f['sha256']:64}  {f['size']:>15}  {f['path']}" for f in files]
    path = dest / REPORT_NAME
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _wait_for_manifest(folder: Path, wait: bool, progress: ProgressFn, cancel,
                       complete: bool = False, stream: Optional[_StripeStream] = None) -> dict:
    while True:
        man = read_manifest(folder)
        if man and (not complete or man.get("status") == STATUS_COMPLETE):
            return man
        if not wait:
            raise TransferError("Manifest ist noch nicht abgeschlossen" if man
                                else f"Kein gültiges Manifest in {folder}")
        if stream:
            progress(stream.consumed, stream.total(), "Warte auf abschließendes Manifest …")
        else:
            progress(0, 0, "Warte auf Manifest …")
        sleep_cancellable(POLL_SECONDS, cancel)


def receive_folder(
    folder: Path,
    dest_parent: Path,
    progress: ProgressFn = null_progress,
    cancel: Optional[threading.Event] = None,
    wait: bool = True,
) -> ReceiveResult:
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    folder = Path(folder)
    dest_parent = Path(dest_parent)

    man = _wait_for_manifest(folder, wait, progress, cancel)
    dest_parent.mkdir(parents=True, exist_ok=True)
    final, partial, state, resumed = _prepare_destination(dest_parent, man)

    need, free = _required_space(man, state), free_space(partial)
    if free < need:
        if not resumed:
            shutil.rmtree(partial, ignore_errors=True)
        raise InsufficientSpaceError(
            f"Nicht genug Speicherplatz auf dem Ziel: benötigt ca. {human_bytes(need)}, "
            f"frei {human_bytes(free)}.")

    if resumed:
        n = len(state.done) + len(state.pending)
        log.info("Resuming receive into %s (%d file(s) already done)", partial, n)
        progress(0, 0, f"Setze Empfang fort ({n} Datei(en) bereits fertig) …")
    else:
        log.info("Receiving %s -> %s", folder, final)

    work = partial / WORK_DIR_NAME
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    stream = _StripeStream(folder, work, progress, cancel, wait)
    try:
        stats = _unpack(stream, partial, state, progress, cancel)
        man = _wait_for_manifest(folder, wait, progress, cancel, complete=True, stream=stream)
        progress(stream.consumed, stream.total(), "Prüfe Dateien …")
        _finalize(partial, state, man)
    except Cancelled:
        log.info("Receive cancelled; it can be resumed from %s", partial)
        raise
    finally:
        stream.close()
        shutil.rmtree(work, ignore_errors=True)

    report = _write_report(partial, man, state, stream.repaired, stats.skipped,
                           started, time.monotonic() - t0)
    (partial / STATE_NAME).unlink(missing_ok=True)
    for path, mtime in reversed(stats.dir_mtimes):
        os.utime(path, (mtime, mtime))
    if final.exists():
        final = unique_path(final)
    os.replace(partial, final)

    total_bytes = sum(f["size"] for f in man["files"])
    msg = (f"Fertig: {len(man['files'])} Dateien ({human_bytes(total_bytes)}) geprüft"
           + (f", {stream.repaired} Stripe(s) repariert" if stream.repaired else ""))
    progress(1, 1, msg)
    log.info("Receive finished: %s, %d files, %d repaired stripe(s), %d skipped (resume)",
             final, len(man["files"]), stream.repaired, stats.skipped)
    return ReceiveResult(final, len(man["files"]), total_bytes, stream.repaired,
                         resumed, stats.skipped, final / report.name)


# ---------------------------------------------------------------- verify only

def verify_transfer(
    folder: Path,
    progress: ProgressFn = null_progress,
    cancel: Optional[threading.Event] = None,
    deep: bool = True,
) -> VerifyResult:
    """Check every packet and (if `deep`) decode the whole stream and check every
    file hash – without writing anything to disk except one temporary stripe."""
    folder = Path(folder)
    man = read_manifest(folder)
    if not man:
        raise TransferError(f"Kein gültiges Manifest in {folder}")
    p = man["params"]
    k, m = p["k"], p["m"]
    complete = man.get("status") == STATUS_COMPLETE
    stripes = man["stripes"] if complete else _highest_stripe(folder) + 1
    res = VerifyResult(man["name"], complete, stripes, 0)

    total, done = stripes * (k + m), 0
    for s in range(stripes):
        valid = 0
        for i in range(k + m):
            check_cancel(cancel)
            progress(done, total, f"Prüfe Stripe {s + 1}/{stripes}, Paket {i + 1}/{k + m} …")
            if _check_packet(folder, man, s, i, cancel):
                valid += 1
            else:
                res.bad_packets.append(packet_name(s, i))
            done += 1
        if valid == k + m:
            res.intact += 1
        elif valid >= k:
            res.degraded.append(s)
        else:
            res.unrecoverable.append(s)
    log.info("Verify %s: %d intact, %d degraded, %d unrecoverable, %d bad packet(s)",
             folder, res.intact, len(res.degraded), len(res.unrecoverable), len(res.bad_packets))

    if not (deep and complete and not res.unrecoverable):
        return res

    work = Path(tempfile.mkdtemp(prefix="vidtransfer-verify-"))
    stream = _StripeStream(folder, work, progress, cancel, wait=False)
    got: dict[str, str] = {}
    try:
        tar, source = _open_tar(stream, man)
        try:
            for member in tar:
                check_cancel(cancel)
                if not member.isfile():
                    continue
                h = hashlib.sha256()
                src = tar.extractfile(member)
                while True:
                    check_cancel(cancel)
                    chunk = src.read(COPY_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                got[member.name] = h.hexdigest()
                res.bytes_checked += member.size
                progress(stream.consumed, stream.total(), f"Prüfe Inhalt: {member.name}")
        finally:
            tar.close()
            if source is not None:
                source.close()
    except (TransferError, tarfile.TarError, zstandard.ZstdError) as exc:
        res.content_errors.append(f"Datenstrom nicht lesbar: {exc}")
    finally:
        stream.close()
        shutil.rmtree(work, ignore_errors=True)

    for f in man["files"]:
        if f["path"] not in got:
            res.content_errors.append(f"fehlt: {f['path']}")
        elif got[f["path"]] != f["sha256"]:
            res.content_errors.append(f"Prüfsumme falsch: {f['path']}")
    res.files_checked = len(got)
    res.content_verified = not res.content_errors
    return res
