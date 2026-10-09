"""Receiver: verify packets → reconstruct if needed → records (v2) / tar (v1) → files.

Stripes are processed in order. If something is still missing for a stripe
(Drive is still syncing, or the sender is still uploading), the receiver waits
until at least k valid packets are present, so receiving can start while the
upload is still running.

Unpacking is crash-safe and resumable:
- everything is written into "<name> (receiving)" first and only renamed to
  "<name>" once every file has been verified;
- each file is written as ".<name>.vtpart" and only gets its real name once its
  SHA-256 matches, so a half-written or corrupt file never appears under its
  real name;
- ".vidtransfer-state.json" records finished files, so an interrupted receive
  continues where it stopped (finished files are not written again).

With format v2 and a complete manifest index, files are processed "targeted":
the receiver jumps from file to file via the index. That is the basis for
restoring single files (`only`) and for partial recovery (`salvage`): if a
stripe is lost for good, only the files that touch it are missing; the result
goes to "<name> (incomplete)" and can be completed later by receiving again.
Without any manifest (`without_manifest`), a stand-in manifest is built from
the packet headers and the records are read sequentially; after damage, the
reader re-synchronises on the next valid record header.
"""

from __future__ import annotations

import fnmatch
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
from pathlib import Path
from typing import Optional

import zstandard

from vidtransfer import __version__
from vidtransfer.container import T_DIR, T_END, T_FILE, ContainerError, RecordHeader, RecordReader
from vidtransfer.erasure import decode_stripe
from vidtransfer.manifest import STATUS_COMPLETE, format_version, read_manifest
from vidtransfer.names import NameMapper
from vidtransfer.packet import HEADER_SIZE, data_len, packet_name, parse_packet_name, read_header
from vidtransfer.prefetch import LOOKAHEAD, PacketVerifier, StripeCheck
from vidtransfer.recovery import synthesize_manifest
from vidtransfer.util import (
    Cancelled, ProgressFn, atomic_write_bytes, check_cancel, free_space, human_bytes,
    human_duration, null_progress, sleep_cancellable, unique_path,
)

log = logging.getLogger(__name__)

POLL_SECONDS = 20
GIVE_UP_SECONDS = 600     # upload complete but a stripe stays short this long → report it
COPY_CHUNK = 1 << 20
SPACE_RESERVE = 1.05

RECEIVING_SUFFIX = " (receiving)"
INCOMPLETE_SUFFIX = " (incomplete)"
STATE_NAME = ".vidtransfer-state.json"
WORK_DIR_NAME = ".vidtransfer-work"
PART_SUFFIX = ".vtpart"
REPORT_NAME = "VidTransfer-Report.txt"
_INTERNAL_NAMES = {STATE_NAME, WORK_DIR_NAME, REPORT_NAME}


class TransferError(Exception):
    pass


class InsufficientSpaceError(TransferError):
    pass


class StripeUnavailableError(TransferError):
    """Fewer than k valid packets for a stripe (and no more waiting)."""

    def __init__(self, stripe: int, valid: int, needed: int, upload_complete: bool):
        super().__init__(f"Stripe {stripe + 1}: nur {valid} von mindestens {needed} gültigen Paketen vorhanden")
        self.stripe = stripe
        self.upload_complete = upload_complete


@dataclass
class ReceiveResult:
    dest: Path
    files: int
    bytes: int
    repaired_stripes: int
    resumed: bool = False
    skipped_files: int = 0
    report: Optional[Path] = None
    complete: bool = True                               # False: some files could not be restored
    missing: list[str] = field(default_factory=list)    # files that could not be restored
    lost_stripes: list[int] = field(default_factory=list)
    renamed: dict[str, str] = field(default_factory=dict)
    selected: bool = False                              # only some files were requested
    without_manifest: bool = False


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
    stalled_packets: list[str] = field(default_factory=list)  # read did not finish within the timeout
    files_checked: int = 0
    bytes_checked: int = 0
    content_verified: bool = False      # stream decoded and every file hash matched
    content_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.complete and not self.unrecoverable and self.content_verified


# ---------------------------------------------------------------- status

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
    """Provides the data stream stripe by stripe as a readable object.

    Packets are verified by a `PacketVerifier` in background threads; the next
    `LOOKAHEAD` stripes are prefetched while the current one is being unpacked.

    `pos` is the absolute stream position of the next byte. `skip_to()` moves it
    forward; stripes that are skipped entirely are never loaded (this is what
    makes resuming and selective restores cheap with the v2 index). All stripes
    except the last are exactly k × block_size bytes, so positions map directly
    to stripes.
    """

    def __init__(self, folder: Path, work: Path, progress: ProgressFn, cancel, wait: bool,
                 verifier: Optional[PacketVerifier] = None, manifest: Optional[dict] = None):
        self.folder = folder
        self.work = work
        self.progress = progress
        self.cancel = cancel
        self.wait = wait
        self.manifest = manifest or read_manifest(folder)
        p = self.manifest["params"]
        self.k, self.m = p["k"], p["m"]
        self._own_verifier = verifier is None
        self.verifier = verifier or PacketVerifier(folder, self.manifest, cancel)
        self.stalled_packets: set[str] = set()
        self.stripe_bytes = self.k * p["block_size"]
        self.stripe = 0              # next stripe to load
        self.pos = 0                 # absolute stream position of the next byte
        self.repaired = 0
        self.loaded_stripes = 0
        self._segments: list[tuple[Path, int, int]] = []  # (file, offset, length)
        self._fh = None
        self._remaining = 0
        self._skip_in_stripe = 0     # bytes to drop at the start of the next loaded stripe
        self._eof = False

    @property
    def consumed(self) -> int:
        return self.pos

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
                self.verifier.update_manifest(new)

    def _complete(self) -> bool:
        return self.manifest.get("status") == STATUS_COMPLETE

    def _load_stripe(self, s: int) -> bool:
        """Prepare stripe `s` for reading. False = end of stream."""
        stuck_since: Optional[float] = None
        while True:
            check_cancel(self.cancel)
            self._refresh_manifest()
            if self._complete() and s >= self.manifest["stripes"]:
                return False

            # Current stripe first, so idle workers do not start on look-ahead packets
            self.verifier.prefetch(s)
            for ahead in range(s + 1, s + 1 + LOOKAHEAD):
                if not (self._complete() and ahead >= self.manifest["stripes"]):
                    self.verifier.prefetch(ahead)

            def on_update(res: StripeCheck) -> None:
                msg = f"Stripe {s + 1}: {len(res.valid)}/{self.k} Pakete bereit, {res.open} werden geladen/geprüft …"
                if res.stalled:
                    msg += f" ({len(res.stalled)} hängt/hängen – nutze Parität)"
                self._report(msg)

            # Parity packets are only requested when needed (saves downloads)
            res = self.verifier.collect(s, on_update=on_update)
            valid = res.valid
            for i in res.stalled:
                if packet_name(s, i) not in self.stalled_packets:
                    self.stalled_packets.add(packet_name(s, i))
                    log.warning("Stripe %d: packet %d stalled, using parity instead", s, i)

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
                self.verifier.forget_before(s)
                return True

            complete = self._complete()
            if not self.wait:
                raise StripeUnavailableError(s, len(valid), self.k, complete)
            if complete:
                stuck_since = stuck_since or time.monotonic()
                if time.monotonic() - stuck_since >= GIVE_UP_SECONDS:
                    log.warning("Stripe %d still has only %d/%d valid packets after %.0f s - giving up",
                                s, len(valid), self.k, GIVE_UP_SECONDS)
                    raise StripeUnavailableError(s, len(valid), self.k, True)
                hint = ("Upload abgeschlossen, aber Pakete fehlen/sind defekt – Drive synchronisiert "
                        "evtl. noch. Notfalls auf dem Sender denselben Ordner erneut senden (repariert).")
            else:
                stuck_since = None
                hint = "Sender lädt noch hoch"
            self._report(f"Warte auf Stripe {s + 1} ({len(valid)}/{self.k} Pakete) – {hint}")
            log.info("Waiting for stripe %d (%d/%d valid)", s, len(valid), self.k)
            sleep_cancellable(POLL_SECONDS, self.cancel)

    # -- file-like interface for zstd/tarfile/RecordReader
    def _drop_from_segments(self, n: int) -> int:
        """Remove up to n bytes from the front of the loaded segments; returns the rest."""
        while n and self._segments:
            path, off, length = self._segments[0]
            if n >= length:
                self._segments.pop(0)
                n -= length
            else:
                self._segments[0] = (path, off + n, length - n)
                n = 0
        return n

    def _next_segment(self) -> bool:
        if self._fh:
            self._fh.close()
            self._fh = None
        while not self._segments:
            if self._eof or not self._load_stripe(self.stripe):
                self._eof = True
                return False
            self.stripe += 1
            self.loaded_stripes += 1
            if self._skip_in_stripe:
                left = self._drop_from_segments(self._skip_in_stripe)
                self._skip_in_stripe = 0
                if left:
                    raise TransferError("Sprungziel liegt hinter dem Ende des Stripes")
        path, off, length = self._segments.pop(0)
        self._fh = open(path, "rb")
        self._fh.seek(off)
        self._remaining = length
        return True

    def skip_to(self, target: int) -> None:
        """Move forward to absolute stream position `target` without loading skipped stripes."""
        n = target - self.pos
        if n < 0:
            raise ValueError("can only skip forward")
        if not n:
            return
        take = min(n, self._remaining)          # 1) within the open segment
        if take:
            self._fh.seek(take, 1)
            self._remaining -= take
            n -= take
        n = self._drop_from_segments(n)         # 2) within the loaded stripe
        if n:                                   # 3) jump to the stripe containing target
            if self._fh:
                self._fh.close()
                self._fh = None
            self._remaining = 0
            self._segments = []
            self._eof = False
            self.stripe = target // self.stripe_bytes
            self._skip_in_stripe = target - self.stripe * self.stripe_bytes
        self.pos = target

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = 1 << 30
        out = bytearray()
        while len(out) < size:
            if self._remaining == 0:
                try:
                    more = self._next_segment()
                except BaseException:
                    self.pos += len(out)    # keep pos in sync with what was consumed
                    raise
                if not more:
                    break
            chunk = self._fh.read(min(size - len(out), self._remaining))
            if not chunk:
                raise TransferError("Paket wurde während des Lesens verändert")
            self._remaining -= len(chunk)
            out += chunk
        self.pos += len(out)
        return bytes(out)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
        if self._own_verifier:
            self.verifier.close()


def _open_tar(stream: _StripeStream, man: dict):
    """Return (tarfile, decompressor-or-None) reading from the stripe stream."""
    source = None
    if man["params"]["compression"] == "zstd":
        source = zstandard.ZstdDecompressor().stream_reader(stream, closefd=False)
    return tarfile.open(fileobj=source or stream, mode="r|"), source


# ---------------------------------------------------------------- receive state

class _ReceiveState:
    """Progress of a receive, stored inside the working folder."""

    def __init__(self, folder: Path, transfer_id: str, done=None, pending=None, names=None):
        self.folder = folder
        self.transfer_id = transfer_id
        # rel path -> {"size", "sha256", "mtime"}
        self.done: dict[str, dict] = done or {}      # verified, under the real name
        self.pending: dict[str, dict] = pending or {}  # v1: written as .vtpart, hash not yet known
        self.names = NameMapper(names)               # transferred path -> local path

    @classmethod
    def load(cls, folder: Path) -> Optional["_ReceiveState"]:
        try:
            raw = json.loads((folder / STATE_NAME).read_text(encoding="utf-8"))
            return cls(folder, raw["transfer_id"], raw.get("done") or {}, raw.get("pending") or {},
                       raw.get("names") or {})
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self) -> None:
        raw = {"transfer_id": self.transfer_id, "app_version": __version__,
               "done": self.done, "pending": self.pending, "names": self.names.mapping}
        atomic_write_bytes(self.folder / STATE_NAME, json.dumps(raw, ensure_ascii=False).encode("utf-8"))

    def target(self, root: Path, rel: str) -> Optional[Path]:
        if not rel or rel.split("/", 1)[0] in _INTERNAL_NAMES:
            return None
        return self.names.target(root, rel)


def _prepare_destination(dest_parent: Path, man: dict) -> tuple[Path, Path, _ReceiveState, bool]:
    """Return (final folder, working folder, state, resumed).

    A "(receiving)" or "(incomplete)" folder of the same transfer is resumed.
    """
    name, tid = man["name"], man["transfer_id"]
    i = 1
    while True:
        final = dest_parent / (name if i == 1 else f"{name} ({i})")
        i += 1
        candidates = [final.with_name(final.name + sfx) for sfx in (RECEIVING_SUFFIX, INCOMPLETE_SUFFIX)]
        for partial in candidates:
            if partial.is_dir():
                state = _ReceiveState.load(partial)
                if state and state.transfer_id == tid:
                    return final, partial, state, True
        if final.exists() or any(c.exists() for c in candidates):
            continue
        partial = candidates[0]
        partial.mkdir()
        state = _ReceiveState(partial, tid)
        state.save()
        return final, partial, state, False


def _select(man: dict, patterns: list[str]) -> list[str]:
    """Paths (in stream order) matching any pattern: glob on the path or the file name,
    or a folder prefix. Case-insensitive, "/" or "\\" as separator."""
    pats = [p.replace("\\", "/").strip().casefold() for p in patterns if p.strip()]
    out = []
    for f in man["files"]:
        path = f["path"].casefold()
        name = path.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(name, p)
               or path.startswith(p.rstrip("/") + "/") for p in pats):
            out.append(f["path"])
    return out


def _required_space(man: dict, state: _ReceiveState, selected: Optional[set[str]]) -> int:
    p = man["params"]
    remaining = sum(f["size"] for f in man["files"]
                    if f["path"] not in state.done and f["path"] not in state.pending
                    and (selected is None or f["path"] in selected))
    return int((remaining + p["k"] * p["block_size"]) * SPACE_RESERVE)


# ---------------------------------------------------------------- unpacking helpers

def _safe_target(root: Path, name: str) -> Optional[Path]:
    """Local path for a transferred path (without collision handling), None if unsafe."""
    if not name or name.split("/", 1)[0] in _INTERNAL_NAMES:
        return None
    return NameMapper().target(root, name)


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


def _drop_changed(state: _ReceiveState, partial: Path) -> None:
    """Forget finished files that were changed or removed in the meantime (they are written again)."""
    for rel, entry in list(state.done.items()):
        target = state.target(partial, rel)
        if target is None or not _unchanged(target, entry):
            del state.done[rel]


def _write_file(reader: RecordReader, rec: RecordHeader, target: Path, expected: Optional[str]) -> dict:
    """Write one v2 file record atomically (.vtpart → real name after the hash check)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path(target)
    try:
        with open(part, "wb") as out:
            sha = reader.read_data(rec, out.write)
            out.flush()
            os.fsync(out.fileno())
        if expected and expected != sha:
            raise ContainerError(f"„{rec.path}“: Prüfsumme passt nicht zum Manifest")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.utime(part, (rec.mtime, rec.mtime))
    os.replace(part, target)
    return {"size": rec.size, "sha256": sha, "mtime": int(rec.mtime)}


def _misread(exc: Exception) -> TransferError:
    # Every packet passed its SHA-256 check, so the data was most likely read
    # incorrectly (transient I/O or memory error). Finished files are kept.
    log.error("Stream could not be decoded: %s", exc)
    return TransferError(f"Datenstrom fehlerhaft gelesen ({exc}). Bitte erneut empfangen – "
                         "fertige Dateien bleiben erhalten.")


@dataclass
class _UnpackStats:
    written: int = 0
    skipped: int = 0
    dir_mtimes: list = field(default_factory=list)
    failed: list = field(default_factory=list)        # files that could not be restored
    lost_stripes: set = field(default_factory=set)
    unknown_damage: bool = False                       # damage where no file name is known


def _unpack(stream: _StripeStream, partial: Path, state: _ReceiveState, progress: ProgressFn,
            cancel, selected: Optional[list[str]], salvage: bool) -> _UnpackStats:
    man = stream.manifest
    if format_version(man) < 2:
        return _unpack_tar(stream, partial, state, progress, cancel)
    indexed = man["files"] and all("offset" in f for f in man["files"])
    if selected is not None or (indexed and not man.get("synthetic")):
        paths = selected if selected is not None else [f["path"] for f in man["files"]]
        return _unpack_targeted(stream, partial, state, progress, cancel, paths, salvage)
    return _unpack_sequential(stream, partial, state, progress, cancel, salvage)


# ---------------------------------------------------------------- v2: targeted (via the index)

def _index_entry(stream: _StripeStream, path: str, cancel) -> dict:
    """Manifest entry with offset for `path`; waits while the sender is still indexing."""
    while True:
        entry = next((f for f in stream.manifest["files"] if f["path"] == path), None)
        if entry is None:
            raise TransferError(f"„{path}“ steht nicht im Manifest")
        if "offset" in entry:
            return entry
        if not stream.wait:
            raise TransferError(f"„{path}“ ist noch nicht übertragen")
        stream.progress(stream.consumed, stream.total(), f"Warte, bis „{path}“ übertragen ist …")
        sleep_cancellable(POLL_SECONDS, cancel)
        stream._refresh_manifest()


def _unpack_targeted(stream: _StripeStream, partial: Path, state: _ReceiveState, progress: ProgressFn,
                     cancel, paths: list[str], salvage: bool) -> _UnpackStats:
    """Jump from file to file via the index. With `salvage`, lost stripes only cost
    the files that touch them."""
    stats = _UnpackStats()
    reader = RecordReader(stream, cancel)
    S = stream.stripe_bytes
    _drop_changed(state, partial)
    try:
        for path in paths:
            check_cancel(cancel)
            if path in state.done:
                stats.skipped += 1
                continue
            entry = _index_entry(stream, path, cancel)
            stripes = range(entry["offset"] // S, (entry["offset"] + entry["length"] - 1) // S + 1)
            if salvage and any(s in stats.lost_stripes for s in stripes):
                stats.failed.append(path)
                continue
            target = state.target(partial, path)
            if target is None:
                log.warning("Skipping unsafe path: %r", path)
                stats.failed.append(path)
                continue
            try:
                if entry["offset"] < reader.pos:      # only after an error inside this stripe
                    raise ContainerError("Position bereits überschritten")
                reader.skip_to(entry["offset"])
                rec = reader.next()
                if rec.type != T_FILE or rec.path != path:
                    raise ContainerError(f"Index passt nicht zum Datenstrom bei „{path}“")
                state.done[path] = _write_file(reader, rec, target, entry.get("sha256"))
            except StripeUnavailableError as exc:
                if not salvage:
                    raise
                log.warning("Stripe %d is lost - %s cannot be restored", exc.stripe, path)
                stats.lost_stripes.add(exc.stripe)
                stats.failed.append(path)
                reader.reset()
                continue
            except ContainerError as exc:
                if not salvage:
                    raise _misread(exc) from exc
                log.warning("%s could not be restored: %s", path, exc)
                stats.failed.append(path)
                reader.reset()
                continue
            state.save()
            stats.written += 1
            progress(stream.consumed, stream.total(), f"Entpackt: {path}")
    finally:
        state.save()
    log.info("Loaded %d stripe(s) for this receive", stream.loaded_stripes)
    return stats


# ---------------------------------------------------------------- v2: sequential

def _resume_position(man: dict, partial: Path, state: _ReceiveState) -> tuple[int, int]:
    """(stream position to start at, number of finished files before it).

    Finished files that were changed or removed in the meantime are dropped from
    the state first, so they are written again.
    """
    _drop_changed(state, partial)
    pos, skipped = 0, 0
    for f in man["files"]:                       # manifest order == stream order
        if f["path"] not in state.done:
            return (f["offset"] if "offset" in f else pos), skipped
        if "offset" not in f:
            break
        pos = f["offset"] + f["length"]
        skipped += 1
    return pos, skipped


def _resync(reader: RecordReader, stream: _StripeStream, start: int, stats: _UnpackStats) -> bool:
    """Continue at the next valid record header at or after `start`, skipping lost stripes."""
    while True:
        try:
            return reader.resync(max(start, stream.pos))
        except StripeUnavailableError as exc:
            stats.lost_stripes.add(exc.stripe)
            start = (exc.stripe + 1) * stream.stripe_bytes


def _unpack_sequential(stream: _StripeStream, partial: Path, state: _ReceiveState,
                       progress: ProgressFn, cancel, salvage: bool) -> _UnpackStats:
    """Read the records in order: while the sender is still indexing, or without manifest."""
    stats = _UnpackStats()
    reader = RecordReader(stream, cancel)
    index_for, index = None, {}
    S = stream.stripe_bytes

    start, stats.skipped = _resume_position(stream.manifest, partial, state)
    if start:
        log.info("Resuming at stream position %d (%d finished file(s) skipped)", start, stats.skipped)
        progress(start, stream.total(), f"Springe zu Position {human_bytes(start)} "
                                        f"({stats.skipped} Datei(en) bereits fertig) …")
        reader.skip_to(start)
    try:
        while True:
            check_cancel(cancel)
            try:
                rec = reader.next()
            except StripeUnavailableError as exc:
                if not salvage:
                    raise
                stats.lost_stripes.add(exc.stripe)
                stats.unknown_damage = True
                if not _resync(reader, stream, (exc.stripe + 1) * S, stats):
                    break
                continue
            except ContainerError as exc:
                if not salvage:
                    raise _misread(exc) from exc
                stats.unknown_damage = True
                if not _resync(reader, stream, stream.pos, stats):
                    break
                continue
            if rec.type == T_END:
                break
            if index_for is not stream.manifest:
                index_for, index = stream.manifest, {f["path"]: f for f in stream.manifest["files"]}
            target = state.target(partial, rec.path)
            if target is None:
                log.warning("Skipping unsafe path: %r", rec.path)
                if rec.type == T_FILE:
                    reader.skip_data(rec, None)
                continue
            if rec.type == T_DIR:
                target.mkdir(parents=True, exist_ok=True)
                stats.dir_mtimes.append((target, rec.mtime))
                continue

            rel = rec.path
            entry = index.get(rel, {})
            if rel in state.done and _unchanged(target, state.done[rel]):
                stats.skipped += 1
                reader.skip_data(rec, entry.get("length"))
                continue
            state.done.pop(rel, None)
            try:
                state.done[rel] = _write_file(reader, rec, target, entry.get("sha256"))
            except StripeUnavailableError as exc:
                if not salvage:
                    raise
                stats.lost_stripes.add(exc.stripe)
                stats.failed.append(rel)
                if not _resync(reader, stream, (exc.stripe + 1) * S, stats):
                    break
                continue
            except ContainerError as exc:
                if not salvage:
                    raise _misread(exc) from exc
                stats.failed.append(rel)
                if not _resync(reader, stream, stream.pos, stats):
                    break
                continue
            state.save()
            stats.written += 1
            progress(stream.consumed, stream.total(), f"Entpackt: {rel}")
    finally:
        state.save()
    log.info("Loaded %d stripe(s) for this receive", stream.loaded_stripes)
    return stats


# ---------------------------------------------------------------- v1: tar stream

def _unpack_tar(stream: _StripeStream, partial: Path, state: _ReceiveState,
                progress: ProgressFn, cancel) -> _UnpackStats:
    """Format v1: zstd-compressed tar stream; hashes come from the manifest."""
    stats = _UnpackStats()
    hashes_for, hashes = None, {}
    tar, source = _open_tar(stream, stream.manifest)
    try:
        for member in tar:
            check_cancel(cancel)
            target = state.target(partial, member.name)
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
    except (zstandard.ZstdError, tarfile.TarError) as exc:
        raise _misread(exc) from exc
    finally:
        tar.close()
        if source is not None:
            source.close()
    return stats


# ---------------------------------------------------------------- finishing

def _finalize(partial: Path, state: _ReceiveState, man: dict,
              selected: Optional[set[str]], failed: set[str]) -> None:
    """Check every requested file against the final manifest; give pending (v1) files their real name."""
    errors = []
    for f in man["files"]:
        rel = f["path"]
        if (selected is not None and rel not in selected) or rel in failed:
            continue
        target = state.target(partial, rel)
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


def _dir_mtimes(partial: Path, man: dict, state: _ReceiveState, stats: _UnpackStats,
                selected: Optional[set[str]]) -> list[tuple[Path, int]]:
    """Directories (deepest first) with their original mtime; creates missing ones (v2).

    With v2, directory records may have been skipped when resuming, so the
    manifest is the source of truth (this also covers empty directories). For a
    selective restore, only the folders that were created are touched.
    """
    if format_version(man) < 2 or man.get("synthetic"):
        out = stats.dir_mtimes
    else:
        out = []
        for d in man.get("dirs", []):
            target = state.target(partial, d["path"])
            if target is None:
                continue
            if selected is None:
                target.mkdir(parents=True, exist_ok=True)
            if target.is_dir():
                out.append((target, d["mtime"]))
    return sorted(out, key=lambda t: len(t[0].parts), reverse=True)


def _write_report(dest: Path, man: dict, state: _ReceiveState, stream: _StripeStream,
                  stats: _UnpackStats, selected: Optional[set[str]], started: datetime,
                  seconds: float) -> Path:
    p = man["params"]
    synthetic = bool(man.get("synthetic"))
    if synthetic:
        files = sorted(({"path": k, **v} for k, v in state.done.items()), key=lambda f: f["path"])
    else:
        files = sorted((f for f in man["files"] if f["path"] in state.done), key=lambda f: f["path"])
    total = sum(f["size"] for f in files)
    incomplete = bool(stats.failed or stats.unknown_damage)
    lines = [
        "VidTransfer receive report",
        "==========================",
        "",
        f"Transfer:          {man['name']}",
        f"Transfer ID:       {man['transfer_id']}",
        f"Sent:              {man.get('created_at', '?')}",
        f"Received:          {started.isoformat(timespec='seconds')}",
        f"Duration:          {human_duration(seconds)}",
        f"Files restored:    {len(files)} ({human_bytes(total)})",
        f"Repaired stripes:  {stream.repaired} of {man.get('stripes', '?')}",
        f"Resumed:           {'yes, ' + str(stats.skipped) + ' file(s) already done' if stats.skipped else 'no'}",
        f"Parameters:        k={p['k']} m={p['m']} packet={human_bytes(p['block_size'])} "
        f"compression={p['compression']}",
        f"App version:       {__version__}",
        "",
    ]
    if selected is not None:
        lines += [f"Only selected files were restored ({len(selected)} requested).", ""]
    if synthetic:
        lines += ["WARNING: no manifest was available. The transfer was read from the packets alone;",
                  "every restored file was verified via its own checksum, but it cannot be checked",
                  "whether files are missing.", ""]
    if incomplete:
        lines += ["INCOMPLETE: some parts of the transfer are lost for good.",
                  f"Lost stripes: {', '.join(str(s + 1) for s in sorted(stats.lost_stripes)) or '?'}"]
        if stats.failed:
            lines += [f"Files that could not be restored ({len(stats.failed)}):"]
            lines += [f"  {path}" for path in sorted(stats.failed)]
        if stats.unknown_damage:
            lines += ["Files inside the damaged area may be missing without being listed."]
        lines += ["To complete it: send the same folder again on the sending side (only missing",
                  "packets are written), then receive into the same destination again.", ""]
    renamed = state.names.renamed()
    if renamed:
        lines += ["Renamed to fit this system:"]
        lines += [f"  {src}  ->  {dst}" for src, dst in sorted(renamed.items())]
        lines += [""]
    lines += ["All restored files were verified with SHA-256.", "",
              f"{'SHA-256':64}  {'Size':>15}  Path"]
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


# ---------------------------------------------------------------- entry point

def receive_folder(
    folder: Path,
    dest_parent: Path,
    progress: ProgressFn = null_progress,
    cancel: Optional[threading.Event] = None,
    wait: bool = True,
    only: Optional[list[str]] = None,
    salvage: bool = False,
    without_manifest: bool = False,
) -> ReceiveResult:
    """Restore a transfer folder into `dest_parent`.

    only:             restore just the files matching these patterns (v2)
    salvage:          if stripes are lost for good, restore everything else into
                      "<name> (incomplete)" instead of failing (v2, upload complete)
    without_manifest: if no manifest is readable, read the packets alone (emergency)
    """
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    folder = Path(folder)
    dest_parent = Path(dest_parent)

    man = read_manifest(folder)
    if man is None and without_manifest:
        man = synthesize_manifest(folder)
        if man is None:
            raise TransferError(f"In {folder} wurden keine VidTransfer-Pakete gefunden")
    elif man is None:
        man = _wait_for_manifest(folder, wait, progress, cancel)
    synthetic = bool(man.get("synthetic"))
    v2 = format_version(man) >= 2
    if (only or salvage) and not v2:
        raise TransferError("Einzelne Dateien und Teilrettung gibt es erst für Transfers ab Format 2.")
    if only and synthetic:
        raise TransferError("Ohne Manifest gibt es keine Dateiliste – bitte alles wiederherstellen.")
    if salvage and man.get("status") != STATUS_COMPLETE:
        raise TransferError("Teilrettung ist erst möglich, wenn der Upload abgeschlossen ist.")
    selected_list = _select(man, only) if only else None
    if only and not selected_list:
        raise TransferError("Keine Datei passt zu: " + ", ".join(only))
    selected = set(selected_list) if selected_list is not None else None

    dest_parent.mkdir(parents=True, exist_ok=True)
    final, partial, state, resumed = _prepare_destination(dest_parent, man)
    need, free = _required_space(man, state, selected), free_space(partial)
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
        log.info("Receiving %s -> %s%s", folder, final, " (without manifest)" if synthetic else "")

    work = partial / WORK_DIR_NAME
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    stream = _StripeStream(folder, work, progress, cancel, wait=wait and not salvage and not synthetic,
                           manifest=man)
    try:
        stats = _unpack(stream, partial, state, progress, cancel, selected_list, salvage or synthetic)
        if not synthetic:
            man = _wait_for_manifest(folder, wait, progress, cancel, complete=True, stream=stream)
            progress(stream.consumed, stream.total(), "Prüfe Dateien …")
            _finalize(partial, state, man, selected, set(stats.failed))
    except Cancelled:
        log.info("Receive cancelled; it can be resumed from %s", partial)
        raise
    finally:
        stream.close()
        shutil.rmtree(work, ignore_errors=True)

    incomplete = bool(stats.failed or stats.unknown_damage)
    report = _write_report(partial, man, state, stream, stats, selected, started, time.monotonic() - t0)
    for path, mtime in _dir_mtimes(partial, man, state, stats, selected):
        os.utime(path, (mtime, mtime))

    if incomplete:
        # Keep the state so a later receive into the same destination can complete it
        dest = final.with_name(final.name + INCOMPLETE_SUFFIX)
        if partial != dest:
            if dest.exists():
                dest = unique_path(dest)
            os.replace(partial, dest)
    else:
        (partial / STATE_NAME).unlink(missing_ok=True)
        dest = final if not final.exists() else unique_path(final)
        os.replace(partial, dest)

    done_files = [f for f in man["files"] if f["path"] in state.done] if not synthetic else \
        [{"size": v["size"]} for v in state.done.values()]
    total_bytes = sum(f["size"] for f in done_files)
    msg = (f"Fertig: {len(done_files)} Dateien ({human_bytes(total_bytes)}) geprüft"
           + (f", {stream.repaired} Stripe(s) repariert" if stream.repaired else "")
           + (f", {len(stats.failed)} Datei(en) nicht rettbar" if stats.failed else ""))
    progress(1, 1, msg)
    log.info("Receive finished: %s, %d files, %d repaired stripe(s), %d skipped (resume), "
             "%d failed, lost stripes %s", dest, len(done_files), stream.repaired, stats.skipped,
             len(stats.failed), sorted(stats.lost_stripes))
    return ReceiveResult(
        dest, len(done_files), total_bytes, stream.repaired, resumed, stats.skipped,
        dest / report.name, complete=not incomplete, missing=sorted(stats.failed),
        lost_stripes=sorted(stats.lost_stripes), renamed=state.names.renamed(),
        selected=selected is not None, without_manifest=synthetic)


# ---------------------------------------------------------------- verify only

def verify_transfer(
    folder: Path,
    progress: ProgressFn = null_progress,
    cancel: Optional[threading.Event] = None,
    deep: bool = True,
    without_manifest: bool = False,
) -> VerifyResult:
    """Check every packet and (if `deep`) decode the whole stream and check every
    file hash – without writing anything to disk except one temporary stripe."""
    folder = Path(folder)
    man = read_manifest(folder)
    if man is None and without_manifest:
        man = synthesize_manifest(folder)
    if not man:
        raise TransferError(f"Kein gültiges Manifest in {folder}")
    p = man["params"]
    k, m = p["k"], p["m"]
    complete = man.get("status") == STATUS_COMPLETE
    stripes = man["stripes"] if complete else _highest_stripe(folder) + 1
    res = VerifyResult(man["name"], complete, stripes, 0)

    # Phase 1: check all k+m packets of every stripe in parallel. All checks are
    # queued at once (ordered by stripe), results are collected stripe by stripe.
    verifier = PacketVerifier(folder, man, cancel)
    try:
        for s in range(stripes):
            for i in range(k + m):
                verifier.request(s, i)
        total = stripes * (k + m)
        for s in range(stripes):
            def on_update(r: StripeCheck, s=s) -> None:
                done = s * (k + m) + len(r.valid) + len(r.bad) + len(r.stalled)
                progress(done, total, f"Prüfe Stripe {s + 1}/{stripes} ({r.open} Pakete in Arbeit) …")

            r = verifier.collect(s, need_all=True, on_update=on_update)
            res.bad_packets += [packet_name(s, i) for i in r.bad]
            res.stalled_packets += [packet_name(s, i) for i in r.stalled]
            if len(r.valid) == k + m:
                res.intact += 1
            elif len(r.valid) >= k:
                res.degraded.append(s)
            else:
                res.unrecoverable.append(s)
        log.info("Verify %s: %d intact, %d degraded, %d unrecoverable, %d bad / %d stalled packet(s)",
                 folder, res.intact, len(res.degraded), len(res.unrecoverable),
                 len(res.bad_packets), len(res.stalled_packets))

        if deep and complete and not res.unrecoverable:
            # Phase 2 reuses the cached packet results, so nothing is hashed twice
            _verify_content(folder, man, res, verifier, progress, cancel)
    finally:
        verifier.close()
    return res


def _hash_records(stream: _StripeStream, res: VerifyResult, got: dict, progress: ProgressFn, cancel) -> None:
    reader = RecordReader(stream, cancel)
    while True:
        check_cancel(cancel)
        rec = reader.next()
        if rec.type == T_END:
            return
        if rec.type == T_FILE:
            got[rec.path] = reader.read_data(rec, lambda _b: None)   # also checks the trailer
            res.bytes_checked += rec.size
            progress(stream.consumed, stream.total(), f"Prüfe Inhalt: {rec.path}")


def _hash_tar(stream: _StripeStream, man: dict, res: VerifyResult, got: dict,
              progress: ProgressFn, cancel) -> None:
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


def _verify_content(folder: Path, man: dict, res: VerifyResult, verifier: PacketVerifier,
                    progress: ProgressFn, cancel) -> None:
    """Decode the whole stream and compare every file hash with the manifest."""
    work = Path(tempfile.mkdtemp(prefix="vidtransfer-verify-"))
    stream = _StripeStream(folder, work, progress, cancel, wait=False, verifier=verifier, manifest=man)
    got: dict[str, str] = {}
    try:
        if format_version(man) >= 2:
            _hash_records(stream, res, got, progress, cancel)
        else:
            _hash_tar(stream, man, res, got, progress, cancel)
    except (TransferError, ContainerError, tarfile.TarError, zstandard.ZstdError) as exc:
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
