"""Diagnostics: what does the operating system say about a file in the cloud folder?

Used to find out whether the cloud client (Google Drive for desktop) exposes the
upload state of a file in a way this app could read. Nothing here changes any
file; it only runs read-only system tools and collects their output in a text
file that can be sent to the developer.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from file_transferer import __version__

IS_MAC = sys.platform == "darwin"
SF_DATALESS = 0x40000000          # macOS: the file content is not stored locally (online only)
MAX_OUTPUT = 6000                 # characters kept per tool


def _run(cmd: list[str], timeout: int = 20) -> str:
    text = "$ " + " ".join(cmd) + "\n"
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return text + "(tool not available)\n"
    except subprocess.TimeoutExpired:
        return text + f"(no answer within {timeout} s)\n"
    body = (out.stdout + out.stderr).strip() or "(no output)"
    if len(body) > MAX_OUTPUT:
        body = body[:MAX_OUTPUT] + f"\n… (cut, {len(body) - MAX_OUTPUT} more characters)"
    return text + body + "\n"


def _describe_file(path: Path) -> str:
    lines = [f"--- {path.name} ---"]
    try:
        st = path.stat()
    except OSError as exc:
        return "\n".join(lines + [f"stat failed: {exc}"]) + "\n"
    modified = datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds")
    lines.append(f"size: {st.st_size} bytes, modified: {modified}")
    blocks = getattr(st, "st_blocks", None)
    if blocks is not None:
        lines.append(f"allocated on disk: {blocks * 512} bytes")
    flags = getattr(st, "st_flags", None)
    if flags is not None:
        lines.append(f"flags: {flags:#x}, online-only (dataless): {'yes' if flags & SF_DATALESS else 'no'}")
    text = "\n".join(lines) + "\n"
    if IS_MAC:
        text += _run(["xattr", "-l", str(path)])
        text += _run(["ls", "-lO@e", str(path)])
        text += _run(["mdls", str(path)])
    return text


def sample_files(path: Path) -> list[Path]:
    """A file itself, or from a delivery folder: the manifest plus the oldest and the
    newest packets (the newest are the ones the cloud client is most likely still uploading)."""
    if path.is_file():
        return [path]
    files = [p for p in path.iterdir() if p.is_file() and not p.name.startswith(".")]
    packets = sorted((p for p in files if p.suffix == ".vtp"), key=lambda p: p.stat().st_mtime)
    chosen = [p for p in files if p.name == "manifest.json"]
    chosen += packets[:2] + [p for p in packets[-3:] if p not in packets[:2]]
    return chosen


def collect(path: Path) -> str:
    when = datetime.now().astimezone().isoformat(timespec="seconds")
    text = [
        "File Transferer diagnostics",
        f"version: {__version__}, time: {when}",
        f"platform: {platform.platform()}, python: {platform.python_version()}",
        f"path: {path}",
        "",
    ]
    if not path.exists():
        return "\n".join(text + ["The path does not exist.", ""])
    if path.is_dir():
        packets = sum(1 for p in path.iterdir() if p.suffix == ".vtp")
        text += [f"folder with {packets} packet files", ""]
    for sample in sample_files(path):
        text.append(_describe_file(sample))
    if IS_MAC:
        text.append("--- file providers on this Mac ---")
        text.append(_run(["fileproviderctl", "listproviders"]))
    else:
        text.append("(The upload state can only be inspected on macOS; run this command on the Mac.)")
    return "\n".join(text) + "\n"


def default_output() -> Path:
    return Path(os.path.expanduser("~")) / "file-transferer-diagnostics.txt"
