"""Mapping transferred paths to safe, portable local paths.

- every path component is normalised to NFC (macOS hands out NFD names);
- on Windows, characters that are not allowed in file names (<>:"|?* and
  control characters), trailing dots/spaces and reserved device names
  (CON, NUL, COM1, …) are replaced;
- on case-insensitive file systems (macOS, Windows), two paths that differ only
  in case would overwrite each other, so the second one gets a " (2)" suffix;
- on Windows, very long paths get the extended-length prefix \\\\?\\.

The mapping is remembered (and stored in the receive state), so a resumed
receive maps every path exactly as before.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Optional

IS_WINDOWS = sys.platform == "win32"
CASE_INSENSITIVE = sys.platform in ("win32", "darwin")
WINDOWS_INVALID = set('<>:"|?*\\')
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
LONG_PATH = 240


def split_safe(name: str) -> Optional[list[str]]:
    """Path components of a transferred path, or None if it could escape the destination."""
    pp = PurePosixPath(name)
    if not pp.parts or pp.is_absolute() or any(p in ("", ".", "..") for p in pp.parts):
        return None
    return list(pp.parts)


def portable_part(part: str, windows: bool = IS_WINDOWS) -> str:
    part = unicodedata.normalize("NFC", part)
    if windows:
        part = "".join("_" if (c in WINDOWS_INVALID or ord(c) < 32) else c for c in part)
        part = part.rstrip(" .") or "_"
        if part.split(".")[0].upper() in WINDOWS_RESERVED:
            part = "_" + part
    return part


def _with_suffix_number(part: str, n: int) -> str:
    stem, dot, ext = part.rpartition(".")
    if not dot or not stem:
        return f"{part} ({n})"
    return f"{stem} ({n}).{ext}"


def fs_path(path: Path) -> Path:
    """On Windows, give very long paths the extended-length prefix."""
    if IS_WINDOWS:
        s = os.path.abspath(path)
        if len(s) >= LONG_PATH and not s.startswith("\\\\?\\"):
            return Path("\\\\?\\" + s)
    return path


class NameMapper:
    """Maps transferred relative paths to local relative paths (deterministic, remembered)."""

    def __init__(self, known: Optional[dict[str, str]] = None,
                 windows: bool = IS_WINDOWS, case_insensitive: bool = CASE_INSENSITIVE):
        self.windows = windows
        self.case_insensitive = case_insensitive
        self.mapping: dict[str, str] = dict(known or {})     # transferred prefix -> local prefix
        self._taken: dict[str, str] = {self._key(v): k for k, v in self.mapping.items()}

    def _key(self, local: str) -> str:
        return local.casefold() if self.case_insensitive else local

    def map(self, name: str) -> Optional[str]:
        """Local relative POSIX path for a transferred path (None if unsafe)."""
        parts = split_safe(name)
        if parts is None:
            return None
        local = ""
        for i in range(len(parts)):
            prefix = "/".join(parts[:i + 1])
            if prefix not in self.mapping:
                candidate = (local + "/" if local else "") + portable_part(parts[i], self.windows)
                n = 2
                base = candidate.rsplit("/", 1)
                while self._taken.get(self._key(candidate), prefix) != prefix:
                    renamed = _with_suffix_number(portable_part(parts[i], self.windows), n)
                    candidate = (base[0] + "/" + renamed) if len(base) == 2 else renamed
                    n += 1
                self.mapping[prefix] = candidate
                self._taken[self._key(candidate)] = prefix
            local = self.mapping[prefix]
        return local

    def target(self, root: Path, name: str) -> Optional[Path]:
        local = self.map(name)
        if local is None:
            return None
        return fs_path(root.joinpath(*local.split("/")))

    def renamed(self) -> dict[str, str]:
        """Transferred paths whose local name differs (for the report)."""
        return {k: v for k, v in self.mapping.items() if k != v}
