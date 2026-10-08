"""Kleine Hilfsfunktionen, die von Sender und Empfänger gemeinsam genutzt werden."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# progress(erledigte_bytes, gesamt_bytes, meldung)
ProgressFn = Callable[[int, int, str], None]


class Cancelled(Exception):
    """Der Benutzer hat den Vorgang abgebrochen."""


def check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def sleep_cancellable(seconds: float, cancel: Optional[threading.Event]) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check_cancel(cancel)
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Schreibt erst in eine temporäre Datei und benennt dann um.

    So sieht der Drive-Client nie eine halb geschriebene Datei unter dem
    endgültigen Namen.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def unique_path(base: Path) -> Path:
    """`base`, oder `base (2)`, `base (3)`, … falls schon vorhanden."""
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
