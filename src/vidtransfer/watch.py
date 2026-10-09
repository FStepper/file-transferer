"""Finding deliveries in the Google Drive folder (for automatic receiving).

The scan only lists file names and reads the small manifests. It never opens
packets, so in Drive's "Stream files" mode nothing large is downloaded just by
looking.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from vidtransfer.manifest import MANIFEST_NAMES, STATUS_COMPLETE, read_manifest
from vidtransfer.packet import parse_packet_name
from vidtransfer.sender import README_NAME

log = logging.getLogger(__name__)


@dataclass
class Found:
    folder: Path
    transfer_id: str
    name: str
    sender: Optional[str]
    size: int                 # original bytes
    files: int
    complete: bool            # the sender has finished
    present: int              # packet files that exist (not verified)
    expected: int             # packets expected (estimate while the sender is running)
    recoverable: bool         # complete, and every stripe has at least k packet files

    @property
    def percent(self) -> int:
        """How much of the delivery has arrived in the Drive folder (by packet count)."""
        if self.complete and self.recoverable:
            return 100
        if not self.expected:
            return 0
        return min(99, int(100 * self.present / self.expected))


def inspect(folder: Path) -> Optional[Found]:
    """Look at one delivery folder; None if it is not (yet) a readable delivery."""
    man = read_manifest(folder)
    if not man:
        return None
    p = man["params"]
    k, m = p["k"], p["m"]
    per_stripe: dict[int, int] = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in names:
        parsed = parse_packet_name(name)
        if parsed:
            per_stripe[parsed[0]] = per_stripe.get(parsed[0], 0) + 1
    complete = man.get("status") == STATUS_COMPLETE
    if complete:
        stripes = man["stripes"]
    else:
        # Upper bound: compressed data can only be smaller than the original
        stripe_bytes = k * p["block_size"]
        stripes = max(1, -(-max(man.get("source_bytes", 0), 1) // stripe_bytes))
    present = sum(min(c, k + m) for s, c in per_stripe.items() if s < stripes)
    recoverable = complete and all(per_stripe.get(s, 0) >= k for s in range(stripes))
    return Found(folder, man["transfer_id"], man.get("name", folder.name), man.get("sender"),
                 man.get("source_bytes", 0), len(man.get("files", [])), complete, present,
                 stripes * (k + m), recoverable)


def scan(root: Path) -> list[Found]:
    """All deliveries directly inside `root` (one level, as written by the sender)."""
    out = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return out
    for entry in entries:
        try:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
        except OSError:
            continue
        found = inspect(Path(entry.path))
        if found:
            out.append(found)
    return out


class NotADeliveryError(Exception):
    pass


def delete_delivery(folder: Path, transfer_id: str) -> None:
    """Delete a delivery folder from the cloud folder – only if it contains nothing but
    VidTransfer's own files of exactly this delivery (so nothing else can be lost)."""
    folder = Path(folder)
    man = read_manifest(folder)
    if not man or man["transfer_id"] != transfer_id:
        raise NotADeliveryError(f"{folder} is not this delivery")
    own = set(MANIFEST_NAMES) | {README_NAME}
    for entry in os.scandir(folder):
        name = entry.name
        ours = (name in own or parse_packet_name(name) is not None
                or (name.startswith(".") and name.endswith(".tmp")))
        if not ours or entry.is_dir():
            raise NotADeliveryError(f"{folder} contains other files ({name}) – not deleted")
    shutil.rmtree(folder)
    log.info("Deleted delivery folder %s", folder)
