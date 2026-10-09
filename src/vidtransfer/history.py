"""The list of deliveries shown in the window, kept across restarts."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from vidtransfer.util import atomic_write_bytes

log = logging.getLogger(__name__)

MAX_ENTRIES = 100

# Delivery states
QUEUED, RUNNING, DONE, PAUSED, INCOMPLETE, FAILED = (
    "queued", "running", "done", "paused", "incomplete", "failed")
ACTIVE = (QUEUED, RUNNING)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Delivery:
    kind: str                                   # "send" or "receive"
    name: str
    sources: list[str]                          # send: folder or files; receive: delivery folder
    target: Optional[str] = None                # send: Drive folder; receive: inbox
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = QUEUED
    message: str = "Wartet …"
    action: Optional[str] = None
    details: str = ""
    hint: str = ""                              # second line, only for the current session
    size: int = 0
    files: int = 0
    sender: Optional[str] = None
    result: Optional[str] = None                # send: delivery folder in Drive; receive: restored folder
    options: dict = field(default_factory=dict)  # receive options (salvage, without_manifest)
    created: str = field(default_factory=now_iso)
    finished: Optional[str] = None


class History:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[Delivery] = []

    @classmethod
    def load(cls, path: Path) -> "History":
        h = cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return h
        names = {f.name for f in fields(Delivery)}
        for item in raw if isinstance(raw, list) else []:
            try:
                d = Delivery(**{k: v for k, v in item.items() if k in names})
            except TypeError:
                continue
            d.hint = ""
            if d.state in ACTIVE:                # the app was closed while it was working
                d.state, d.message, d.action = PAUSED, "Unterbrochen.", "resume"
            h.items.append(d)
        return h

    def save(self) -> None:
        del self.items[MAX_ENTRIES:]
        raw = json.dumps([asdict(d) for d in self.items], ensure_ascii=False, indent=1)
        try:
            atomic_write_bytes(self.path, raw.encode("utf-8"))
        except OSError:
            log.exception("Could not save the delivery list")

    def add(self, d: Delivery) -> Delivery:
        self.items.insert(0, d)
        self.save()
        return d

    def get(self, delivery_id: str) -> Optional[Delivery]:
        return next((d for d in self.items if d.id == delivery_id), None)

    def of_kind(self, kind: str) -> list[Delivery]:
        return [d for d in self.items if d.kind == kind]

    def remove_finished(self) -> int:
        before = len(self.items)
        self.items = [d for d in self.items if d.state not in (DONE,)]
        self.save()
        return before - len(self.items)
