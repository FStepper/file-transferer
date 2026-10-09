"""Parallel packet prefetching and verification for the receiver.

Reading a packet is what makes Google Drive (in "Stream files" mode) download
it, so packets are read and hashed by a small pool of background threads ahead
of the stripe that is currently being unpacked. hashlib releases the GIL, so
hashing runs on several cores at once.

A read on a Drive placeholder can block for a long time and cannot be
interrupted. A check that has made no progress for `PACKET_TIMEOUT` seconds is
therefore treated as *stalled*: the stripe continues with parity packets
instead, and an extra worker thread is started so the queue keeps moving. The
stalled read keeps running in its (daemon) thread; if it finishes later, its
result is used.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from vidtransfer.packet import PacketHeader, packet_name, verify_packet
from vidtransfer.util import Cancelled, check_cancel

log = logging.getLogger(__name__)

PACKET_TIMEOUT = 300.0   # seconds without read progress before a packet counts as stalled
WORKERS = 4              # parallel reads/hashes
MAX_WORKERS = 16         # upper bound including replacements for stalled threads
LOOKAHEAD = 2            # stripes prefetched ahead of the current one
WAIT_SLICE = 0.5         # how often waiting consumers re-evaluate stalls/cancel

_PENDING, _RUNNING, _DONE = range(3)


def file_signature(path: Path) -> Optional[tuple[int, int]]:
    """(size, mtime_ns), or None if the file does not exist. Cheap: no download."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def check_packet(folder: Path, man: dict, s: int, i: int, cancel=None,
                 on_progress: Optional[Callable[[int], None]] = None) -> Optional[PacketHeader]:
    """Fully verify one packet (header, size, SHA-256) against the manifest."""
    p = man["params"]
    path = folder / packet_name(s, i)
    if not path.exists():
        return None
    hdr = verify_packet(path, cancel, on_progress)
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


@dataclass(eq=False)
class _Check:
    stripe: int
    index: int
    sig: Optional[tuple[int, int]]
    state: int = _PENDING
    header: Optional[PacketHeader] = None
    last_progress: float = 0.0
    replaced: bool = False       # an extra worker was started because this check stalled
    retry: bool = False          # forced re-check of a packet that failed before

    def stalled(self, now: float, timeout: float) -> bool:
        return self.state == _RUNNING and now - self.last_progress > timeout


@dataclass
class StripeCheck:
    """Result of collecting one stripe."""
    valid: dict[int, PacketHeader] = field(default_factory=dict)
    bad: list[int] = field(default_factory=list)       # missing or damaged
    stalled: list[int] = field(default_factory=list)   # read made no progress within the timeout
    open: int = 0                                      # checks still queued or running


class PacketVerifier:
    """Verifies packets in background threads; results are cached per packet."""

    def __init__(self, folder: Path, manifest: dict, cancel: Optional[threading.Event] = None,
                 workers: Optional[int] = None, timeout: Optional[float] = None):
        self.folder = Path(folder)
        self.manifest = manifest
        self.cancel = cancel
        p = manifest["params"]
        self.k, self.m = p["k"], p["m"]
        self.timeout = PACKET_TIMEOUT if timeout is None else timeout
        self._cond = threading.Condition()
        self._heap: list = []
        self._seq = itertools.count()
        self._checks: dict[tuple[int, int], _Check] = {}
        self._generation = 0     # bumped whenever a check finishes
        self._threads = 0
        self._closed = False
        for _ in range(WORKERS if workers is None else workers):
            self._spawn()

    # ------------------------------------------------------------ pool
    def _spawn(self) -> None:
        if self._threads >= MAX_WORKERS:
            return
        self._threads += 1
        threading.Thread(target=self._worker, name=f"vt-verify-{self._threads}", daemon=True).start()

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._heap and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                *_, check = heapq.heappop(self._heap)
                if check.state != _PENDING:
                    continue
                check.state = _RUNNING
                check.last_progress = time.monotonic()
                man = self.manifest

            def on_progress(_n: int, c: _Check = check) -> None:
                c.last_progress = time.monotonic()

            try:
                hdr = check_packet(self.folder, man, check.stripe, check.index, self.cancel, on_progress)
            except Cancelled:
                hdr = None
            except Exception:  # noqa: BLE001 – a broken packet must not kill the pool
                log.exception("Checking packet %s failed", packet_name(check.stripe, check.index))
                hdr = None
            with self._cond:
                check.header = hdr
                check.state = _DONE
                self._generation += 1
                if check.replaced:
                    log.info("Stalled packet %s finished after all (%s)",
                             packet_name(check.stripe, check.index), "valid" if hdr else "invalid")
                if check.retry and hdr:
                    log.warning("Packet %s is valid on re-read - the previous read was faulty "
                                "(transient I/O or memory error)", packet_name(check.stripe, check.index))
                self._cond.notify_all()

    def close(self) -> None:
        """Stop idle workers. Threads stuck in a blocking read are daemons and end with it."""
        with self._cond:
            self._closed = True
            self._heap.clear()
            self._cond.notify_all()

    # ------------------------------------------------------------ requests
    def update_manifest(self, manifest: dict) -> None:
        with self._cond:
            self.manifest = manifest

    def request(self, s: int, i: int, force: bool = False) -> None:
        """Queue a check of packet (s, i) unless an up-to-date result exists or one is running.

        A finished result is reused as long as the file is unchanged; if the file
        appeared, disappeared or changed (Drive synced a new version), it is checked
        again. `force` re-checks a finished result even if the file is unchanged.
        """
        sig = file_signature(self.folder / packet_name(s, i))
        with self._cond:
            old = self._checks.get((s, i))
            if old and (old.state != _DONE or (old.sig == sig and not force)):
                return
            check = _Check(s, i, sig, retry=force)
            self._checks[(s, i)] = check
            heapq.heappush(self._heap, (s, i, next(self._seq), check))
            self._cond.notify()

    def prefetch(self, s: int) -> None:
        """Queue the data packets of stripe `s` (lower priority than earlier stripes)."""
        for i in range(self.k):
            self.request(s, i)

    def forget_before(self, s: int) -> None:
        with self._cond:
            for key in [key for key, c in self._checks.items() if key[0] < s and c.state == _DONE]:
                del self._checks[key]

    # ------------------------------------------------------------ results
    def _snapshot(self, s: int, asked: set[int]) -> StripeCheck:
        """Current state of the asked packets of stripe `s` (call with the lock held)."""
        now = time.monotonic()
        res = StripeCheck()
        for i in sorted(asked):
            c = self._checks.get((s, i))
            if c is None:
                continue
            if c.state == _DONE:
                if c.header:
                    res.valid[i] = c.header
                else:
                    res.bad.append(i)
            elif c.stalled(now, self.timeout):
                res.stalled.append(i)
                if not c.replaced:
                    c.replaced = True
                    log.warning("Packet %s made no progress for %.0f s - continuing without it",
                                packet_name(s, i), self.timeout)
                    self._spawn()
            else:
                res.open += 1
        return res

    def collect(self, s: int, need_all: bool = False,
                on_update: Optional[Callable[[StripeCheck], None]] = None) -> StripeCheck:
        """Wait until stripe `s` can be assembled, or until nothing more can be done.

        Normal mode: data packets are checked first; for every data packet that is
        missing, damaged or stalled, one more parity packet is requested. Returns as
        soon as k packets are valid, or when every requested check is settled.

        `need_all`: check all k+m packets (used by "verify only").

        A packet that failed is read once more before giving up (normal mode: only
        if the stripe is short of valid packets), because a failed hash can also be
        caused by a transient read error rather than a damaged file.
        """
        k, m = self.k, self.m
        asked = set(range(k + m if need_all else k))
        for i in sorted(asked):
            self.request(s, i)
        next_parity = k + m if need_all else k
        retried: set[int] = set()
        while True:
            check_cancel(self.cancel)
            with self._cond:
                res = self._snapshot(s, asked)
                generation = self._generation
            if not need_all:
                more = []
                while len(res.valid) + res.open + len(more) < k and next_parity < k + m:
                    more.append(next_parity)
                    next_parity += 1
                if more:
                    asked.update(more)
                    for i in more:
                        self.request(s, i)
                    continue
                if len(res.valid) >= k:
                    return res
            if res.open == 0:
                again = [i for i in res.bad if i not in retried]
                if again:
                    retried.update(again)
                    for i in again:
                        self.request(s, i, force=True)
                    continue
                return res
            if on_update:
                on_update(res)
            with self._cond:
                if self._generation == generation:   # nothing finished since the snapshot
                    self._cond.wait(WAIT_SLICE)
