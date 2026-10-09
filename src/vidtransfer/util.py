"""Small helpers shared by sender and receiver."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# progress(done_bytes, total_bytes, message)
ProgressFn = Callable[[int, int, str], None]


class Cancelled(Exception):
    """The user cancelled the operation."""


def check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def sleep_cancellable(seconds: float, cancel: Optional[threading.Event]) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check_cancel(cancel)
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write to a temporary file first, then rename.

    This way the Drive client never sees a half-written file under its final name.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def free_space(path: Path) -> int:
    """Free bytes on the volume containing `path` (or its nearest existing parent)."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def unique_path(base: Path) -> Path:
    """`base`, or `base (2)`, `base (3)`, … if it already exists."""
    if not base.exists():
        return base
    i = 2
    while True:
        cand = base.with_name(f"{base.name} ({i})")
        if not cand.exists():
            return cand
        i += 1


def null_progress(done: int, total: int, msg: str) -> None:
    pass
