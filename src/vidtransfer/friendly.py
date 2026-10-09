"""Plain-language texts for the window.

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
    "stop": "Stop",
    "resume": "Resume",
    "retry": "Try again",
    "resend": "Send again",
    "reveal": "Show in Explorer" if sys.platform == "win32" else "Show in Finder",
    "open_drive": "Start cloud app",
    "choose_inbox": "Choose another location",
    "open_privacy": "Allow access …",
    "salvage": "Get everything else now",
    "fetch_missing": "Get missing files",
    "choose_drive": "Choose cloud folder …",
    "receive_now": "Receive",
}


@dataclass
class Explanation:
    message: str                 # one sentence for the card
    action: Optional[str]        # key from ACTIONS
    details: str                 # technical text for "Details"


def when(iso: Optional[str]) -> str:
    """"today 14:02", "yesterday 18:40" or "2026-10-03 09:15"."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return ""
    today = datetime.now().astimezone().date()
    if dt.date() == today:
        return f"today {dt:%H:%M}"
    if (today - dt.date()).days == 1:
        return f"yesterday {dt:%H:%M}"
    return f"{dt:%Y-%m-%d %H:%M}"


def remaining(seconds: float) -> str:
    if seconds < 60:
        return "less than 1 min left"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"about {minutes} min left"
    hours, minutes = divmod(minutes, 60)
    return f"about {hours} h {minutes} min left" if minutes else f"about {hours} h left"


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


def running_text(kind: str, done: int, total: int, msg: str, eta: Eta,
                 incoming: Optional[int] = None) -> str:
    """Status line while a job is running (the engine's technical message is not shown).

    `incoming`: how much of the delivery has arrived in the Drive folder (percent),
    shown while receiving waits for the rest.
    """
    if kind == "receive" and msg.startswith("Waiting"):
        if incoming is not None and incoming < 100:
            return f"Still uploading ({incoming} %) – receiving as it arrives …"
        return "Waiting for the cloud – the delivery is not complete yet …"
    verb = "Preparing" if kind == "send" else "Receiving"
    if total <= 0:
        return f"{verb} …"
    pct = min(100, int(100 * done / total))
    rest = eta.text(done, total)
    return f"{verb} – {pct} %" + (f" · {rest}" if rest else "")


def size_line(size: int, files: int, sender: Optional[str] = None) -> str:
    parts = []
    if sender:
        parts.append(f"from {sender}")
    if size:
        parts.append(human_bytes(size))
    if files:
        parts.append(f"{files} file" + ("" if files == 1 else "s"))
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
        return e("Paused.", "resume")
    if isinstance(exc, DriveFolderMissingError):
        return e("The cloud folder cannot be reached. Is the cloud app running?", "open_drive")
    if isinstance(exc, SourceMissingError):
        return e("The folder or file is gone – was something moved "
                 "or a drive disconnected?", "retry")
    if isinstance(exc, SourceChangedError):
        return e("A file changed while it was being sent.", "resend")
    if isinstance(exc, InsufficientSpaceError):
        missing = max(0, exc.needed - exc.free)
        return e(f"This computer needs {human_bytes(missing)} more free space.", "choose_inbox")
    if isinstance(exc, StripeUnavailableError):
        if exc.upload_complete:
            return e("Part of the delivery is missing in the cloud.", "salvage")
        return e("The delivery is not complete in the cloud yet.", "retry")
    if isinstance(exc, PermissionError):
        return e("VidTransfer is not allowed to access a folder.", "open_privacy")
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return e("The disk is full – on this computer or in the cloud.", "retry")
    if isinstance(exc, TransferError):
        if "manifest" in str(exc).lower():
            return e("This delivery has not fully arrived in the cloud yet.", "retry")
        return e("A read error occurred while receiving. Try again continues "
                 "where it stopped.", "retry")
    verb = "sending" if kind == "send" else "receiving"
    return e(f"Something went wrong while {verb}.", "resend" if kind == "send" else "retry")
