"""Manifest: description of a transfer, stored three times in the Drive folder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from file_transferer.util import atomic_write_bytes

FORMAT = "vidtransfer"   # format identifier, kept for compatibility
FORMAT_VERSION = 2              # 1 = zstd-compressed tar stream, 2 = record container
SUPPORTED_VERSIONS = (1, 2)
MANIFEST_NAMES = ("manifest.json", "manifest.2.json", "manifest.3.json")

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETE = "complete"


def _checksum(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_manifest(folder: Path, data: dict) -> None:
    body = {k: v for k, v in data.items() if k != "checksum"}
    body["format"] = FORMAT
    body.setdefault("format_version", FORMAT_VERSION)
    body["checksum"] = _checksum(body)
    raw = json.dumps(body, indent=1, sort_keys=True, ensure_ascii=False).encode("utf-8")
    for name in MANIFEST_NAMES:
        atomic_write_bytes(folder / name, raw)


def _load_one(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    if data.get("format_version") not in SUPPORTED_VERSIONS:
        return None
    body = {k: v for k, v in data.items() if k != "checksum"}
    return data if data.get("checksum") == _checksum(body) else None


def read_manifest(folder: Path) -> Optional[dict]:
    """First valid copy; a completed copy takes precedence.

    (While Drive is syncing, the three copies may briefly differ.)
    """
    found = [m for m in (_load_one(folder / n) for n in MANIFEST_NAMES) if m]
    for m in found:
        if m.get("status") == STATUS_COMPLETE:
            return m
    return found[0] if found else None


def format_version(man: dict) -> int:
    return man.get("format_version", 1)


def is_transfer_folder(folder: Path) -> bool:
    """A folder with a manifest – or at least packets (emergency recovery)."""
    if not folder.is_dir():
        return False
    if any((folder / n).exists() for n in MANIFEST_NAMES):
        return True
    try:
        return any(p.suffix == ".vtp" for p in folder.iterdir())
    except OSError:
        return False
