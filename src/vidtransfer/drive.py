"""Locates the local folder of Google Drive for desktop."""

from __future__ import annotations

import string
import sys
from pathlib import Path

# Name of the root folder, depending on the Drive client's language
MY_DRIVE_NAMES = ("My Drive", "Meine Ablage")


def find_drive_roots() -> list[Path]:
    roots: list[Path] = []
    home = Path.home()
    if sys.platform == "darwin":
        cloud = home / "Library" / "CloudStorage"
        if cloud.is_dir():
            for account in sorted(cloud.glob("GoogleDrive-*")):
                roots += [account / n for n in MY_DRIVE_NAMES if (account / n).is_dir()]
        legacy = Path("/Volumes/GoogleDrive")
        roots += [legacy / n for n in MY_DRIVE_NAMES if (legacy / n).is_dir()]
    elif sys.platform == "win32":
        for letter in string.ascii_uppercase:
            base = Path(f"{letter}:\\")
            for n in MY_DRIVE_NAMES:
                try:
                    if (base / n).is_dir():
                        roots.append(base / n)
                except OSError:
                    pass
    # Mirror mode / older clients
    for n in (*MY_DRIVE_NAMES, "Google Drive"):
        if (home / n).is_dir():
            roots.append(home / n)
    seen, unique = set(), []
    for r in roots:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def default_cloudstorage_dir() -> Path:
    cloud = Path.home() / "Library" / "CloudStorage"
    return cloud if cloud.is_dir() else Path.home()
