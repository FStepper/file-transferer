"""Emergency helpers and file listing.

`synthesize_manifest` rebuilds the information needed to read a transfer from
the packet headers alone, for the case that all three manifest copies are lost.
Without the manifest there is no file list and no index, so completeness
cannot be checked – but every v2 file still carries its own SHA-256 in its
record trailer, so each restored file is still verified.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from vidtransfer.manifest import STATUS_COMPLETE, read_manifest
from vidtransfer.packet import HEADER_SIZE, block_size, data_len, packet_name, parse_packet_name, read_header

log = logging.getLogger(__name__)

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
RECORD_MAGIC = b"VTRC"


def has_packets(folder: Path) -> bool:
    try:
        return any(parse_packet_name(p.name) for p in Path(folder).iterdir())
    except OSError:
        return False


def synthesize_manifest(folder: Path) -> Optional[dict]:
    """A stand-in manifest built from packet headers, or None if there are no packets."""
    folder = Path(folder)
    headers = []
    for p in folder.iterdir():
        parsed = parse_packet_name(p.name)
        if parsed:
            hdr = read_header(p)
            if hdr and (hdr.stripe, hdr.index) == parsed:
                headers.append(hdr)
    if not headers:
        return None
    tid = Counter(h.transfer_id for h in headers).most_common(1)[0][0]
    headers = [h for h in headers if h.transfer_id == tid]
    k, m = headers[0].k, headers[0].m
    lens = {}
    for h in headers:
        lens.setdefault(h.stripe, h.stripe_len)
    stripes = max(lens) + 1
    # Every stripe except the last is exactly k * block_size bytes
    full = max(lens.values())
    bsize = full // k if stripes > 1 else block_size(full, k)
    version, compression = _detect_format(folder, headers, k)
    log.warning("No manifest in %s - synthesized one from %d packet headers (%d stripes, k=%d, m=%d, v%d)",
                folder, len(headers), stripes, k, m, version)
    return {
        "format_version": version,
        "transfer_id": tid.hex(),
        "name": folder.name,
        "status": STATUS_COMPLETE,
        "params": {"k": k, "m": m, "block_size": bsize, "compression": compression, "zstd_level": 0},
        "stripes": stripes,
        "stripe_lens": [lens.get(s, full) for s in range(stripes)],
        "files": [],
        "dirs": [],
        "source_bytes": 0,
        "synthetic": True,
    }


def _detect_format(folder: Path, headers, k: int) -> tuple[int, str]:
    """Look at the first bytes of the stream (data packet 0 of stripe 0)."""
    first = next((h for h in headers if h.stripe == 0 and h.index == 0), None)
    head = b""
    if first and data_len(first.stripe_len, k, 0) >= 4:
        with open(folder / packet_name(0, 0), "rb") as f:
            f.seek(HEADER_SIZE)
            head = f.read(4)
    if head == RECORD_MAGIC:
        return 2, "per-file"
    if head == ZSTD_MAGIC:
        return 1, "zstd"
    if head:
        return 1, "none"
    return 2, "per-file"   # packet 0 missing: v2 is the current format


# ---------------------------------------------------------------- listing

@dataclass
class FileInfo:
    path: str
    size: int
    compression: str            # "raw", "zstd" or "?" (v1)
    stripes: Optional[tuple[int, int]]   # first/last stripe, None if not indexed yet
    available: Optional[bool]   # all its stripes have ≥ k packets (quick check); None = unknown


def list_transfer(folder: Path) -> tuple[Optional[dict], list[FileInfo]]:
    """File list from the manifest index, with a quick availability check per file."""
    from vidtransfer.receiver import transfer_status

    man, status = transfer_status(folder)
    if not man:
        return None, []
    ok = {s.stripe for s in status if s.recoverable}
    stripe_bytes = man["params"]["k"] * man["params"]["block_size"]
    out = []
    for f in man["files"]:
        if "offset" in f:
            first = f["offset"] // stripe_bytes
            last = (f["offset"] + f["length"] - 1) // stripe_bytes
            rng = (first, last)
            available = all(s in ok for s in range(first, last + 1))
        else:
            rng, available = None, None
        out.append(FileInfo(f["path"], f["size"], f.get("compression", "?"), rng, available))
    return man, out


def read_any_manifest(folder: Path, allow_synthetic: bool) -> Optional[dict]:
    man = read_manifest(folder)
    if man is None and allow_synthetic:
        man = synthesize_manifest(folder)
    return man
