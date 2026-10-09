"""Settings (JSON) and log file in the user's profile."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

from vidtransfer import APP_NAME
from vidtransfer.sender import MIB, SendOptions
from vidtransfer.util import atomic_write_bytes

# Overrides the settings directory, e.g. for tests or a separate local test setup
CONFIG_DIR_ENV = "VIDTRANSFER_CONFIG_DIR"


def config_dir() -> Path:
    if os.environ.get(CONFIG_DIR_ENV):
        d = Path(os.environ[CONFIG_DIR_ENV])
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_dir() -> Path:
    if sys.platform == "darwin" and not os.environ.get(CONFIG_DIR_ENV):
        d = Path.home() / "Library" / "Logs" / APP_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d
    return config_dir()


@dataclass
class Config:
    mode: str = "send"                        # last mode of the window: "send" or "receive"
    sender_name: Optional[str] = None         # shown to the receiver; default: computer name
    drive_root: Optional[str] = None          # target folder for sending (Google Drive or a local test folder)
    receive_dir: Optional[str] = None         # inbox for received deliveries (default: desktop.default_inbox())
    auto_receive: bool = True                 # mode "Receive" fetches new deliveries by itself
    auto_initialized: bool = False            # deliveries already in Drive were offered once
    last_send_dir: Optional[str] = None       # folder last opened in the "send folder" dialog
    last_transfer_dir: Optional[str] = None   # folder last opened in the "receive"/"verify" dialogs
    k: int = 10
    m: int = 3
    block_mib: int = 32
    zstd_level: int = 3

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "config.json"

    @classmethod
    def load(cls) -> "Config":
        try:
            raw = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in names})

    def save(self) -> None:
        raw = json.dumps(asdict(self), indent=2, ensure_ascii=False).encode("utf-8")
        atomic_write_bytes(self.path(), raw)

    def update(self, **changes) -> None:
        """Save only the given settings, merged into the current file.

        Several app windows may be open at the same time (on Windows every folder
        dropped onto the exe starts a new one). Saving the whole in-memory config
        would overwrite settings another window changed in the meantime.
        """
        names = {f.name for f in fields(self)}
        unknown = set(changes) - names
        if unknown:
            raise KeyError(f"unknown settings: {sorted(unknown)}")
        fresh = Config.load()
        for key, value in changes.items():
            setattr(fresh, key, value)
        fresh.save()
        for f in fields(self):
            setattr(self, f.name, getattr(fresh, f.name))

    def history_path(self) -> Path:
        return config_dir() / "history.json"

    def seen_path(self) -> Path:
        return config_dir() / "received.json"

    def send_options(self) -> SendOptions:
        return SendOptions(k=self.k, m=self.m, block_size=self.block_mib * MIB, zstd_level=self.zstd_level)


def setup_logging(verbose: bool = False) -> Path:
    path = log_dir() / "vidtransfer.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5 * MIB, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)
    return path
