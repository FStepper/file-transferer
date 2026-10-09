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


def config_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_dir() -> Path:
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Logs" / APP_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d
    return config_dir()


@dataclass
class Config:
    drive_root: Optional[str] = None
    receive_dir: Optional[str] = None
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
        self.path().write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

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
