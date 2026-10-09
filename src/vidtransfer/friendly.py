"""Plain-language texts for the window (UI language: German).

Everything a non-technical user reads in the main window comes from here:
status lines, remaining time and the error catalogue. Technical details are
only shown on request ("Details").
"""

from __future__ import annotations

import errno
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from vidtransfer.util import Cancelled, human_bytes

# Actions a card can offer (key -> button text)
ACTIONS = {
    "stop": "Stopp",
    "resume": "Fortsetzen",
    "retry": "Erneut versuchen",
    "resend": "Erneut senden",
    "reveal": "Im Explorer zeigen" if sys.platform == "win32" else "Im Finder zeigen",
    "open_drive": "Google Drive öffnen",
    "choose_inbox": "Anderen Speicherort wählen",
    "open_privacy": "Zugriff erlauben …",
    "salvage": "Alles andere jetzt holen",
    "fetch_missing": "Fehlende Dateien holen",
}


@dataclass
class Explanation:
    message: str                 # one sentence for the card
    action: Optional[str]        # key from ACTIONS
    details: str                 # technical text for "Details"


def when(iso: Optional[str]) -> str:
    """"heute 14:02", "gestern 18:40" or "03.10. 09:15"."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return ""
    today = datetime.now().astimezone().date()
    if dt.date() == today:
        return f"heute {dt:%H:%M}"
    if (today - dt.date()).days == 1:
        return f"gestern {dt:%H:%M}"
    return f"{dt:%d.%m.} {dt:%H:%M}"


def remaining(seconds: float) -> str:
    if seconds < 60:
        return "noch weniger als 1 Min."
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"noch ca. {minutes} Min."
    hours, minutes = divmod(minutes, 60)
    return f"noch ca. {hours} Std. {minutes} Min." if minutes else f"noch ca. {hours} Std."


class Eta:
    """Remaining time from the average throughput since the start."""

    MIN_SECONDS = 5

    def __init__(self):
        self.start = time.monotonic()
        self.base: Optional[int] = None

    def text(self, done: int, total: int) -> str:
        if self.base is None:
            self.base = done                      # resumed jobs may start above 0
            self.start = time.monotonic()
        elapsed = time.monotonic() - self.start
        progressed = done - self.base
        if total <= 0 or progressed <= 0 or elapsed < self.MIN_SECONDS or done >= total:
            return ""
        return remaining((total - done) * elapsed / progressed)


def running_text(kind: str, done: int, total: int, msg: str, eta: Eta) -> str:
    """Status line while a job is running (the engine's technical message is not shown)."""
    if kind == "receive" and msg.startswith("Warte"):
        return "Wartet auf Google Drive – die Lieferung ist noch nicht vollständig da …"
    verb = "Wird vorbereitet" if kind == "send" else "Wird empfangen"
    if total <= 0:
        return f"{verb} …"
    pct = min(100, int(100 * done / total))
    rest = eta.text(done, total)
    return f"{verb} – {pct} %" + (f" · {rest}" if rest else "")


def size_line(size: int, files: int, sender: Optional[str] = None) -> str:
    parts = []
    if sender:
        parts.append(f"von {sender}")
    if size:
        parts.append(human_bytes(size))
    if files:
        parts.append(f"{files} Datei" + ("" if files == 1 else "en"))
    return " · ".join(parts)


def explain(exc: BaseException, kind: str) -> Explanation:
    """Error catalogue: one plain sentence and one action for each known problem."""
    from vidtransfer.container import SourceChangedError
    from vidtransfer.receiver import InsufficientSpaceError, StripeUnavailableError, TransferError
    from vidtransfer.sender import DriveFolderMissingError, SourceMissingError

    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()

    def e(message: str, action: Optional[str]) -> Explanation:
        return Explanation(message, action, details)

    if isinstance(exc, Cancelled):
        return e("Pausiert.", "resume")
    if isinstance(exc, DriveFolderMissingError):
        return e("Der Google-Drive-Ordner ist nicht erreichbar. Läuft Google Drive?", "open_drive")
    if isinstance(exc, SourceMissingError):
        return e("Der Ordner bzw. die Datei ist nicht mehr da – wurde etwas verschoben "
                 "oder ein Laufwerk getrennt?", "retry")
    if isinstance(exc, SourceChangedError):
        return e("Eine Datei hat sich während des Sendens verändert.", "resend")
    if isinstance(exc, InsufficientSpaceError):
        missing = max(0, exc.needed - exc.free)
        return e(f"Auf diesem Computer fehlen {human_bytes(missing)} Speicherplatz.", "choose_inbox")
    if isinstance(exc, StripeUnavailableError):
        if exc.upload_complete:
            return e("Ein Teil der Lieferung fehlt in Google Drive.", "salvage")
        return e("Die Lieferung ist noch nicht vollständig in Google Drive.", "retry")
    if isinstance(exc, PermissionError):
        return e("VidTransfer darf auf einen Ordner nicht zugreifen.", "open_privacy")
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return e("Der Speicherplatz ist voll – auf diesem Computer oder in Google Drive.", "retry")
    if isinstance(exc, TransferError):
        if "Manifest" in str(exc):
            return e("Diese Lieferung ist noch nicht vollständig in Google Drive angekommen.", "retry")
        return e("Beim Empfangen ist ein Lesefehler aufgetreten. Erneut versuchen setzt "
                 "dort fort, wo es aufgehört hat.", "retry")
    verb = "Senden" if kind == "send" else "Empfangen"
    return e(f"Beim {verb} ist etwas schiefgelaufen.", "resend" if kind == "send" else "retry")
