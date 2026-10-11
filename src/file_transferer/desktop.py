"""Small platform helpers for the desktop app (Finder/Explorer, notifications, …)."""

from __future__ import annotations

import logging
import os
import plistlib
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"


def computer_name() -> str:
    """The name of this computer as the user knows it (e.g. "Kamera-Mac")."""
    if IS_MAC:
        try:
            out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True,
                                 text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if IS_WIN and os.environ.get("COMPUTERNAME"):
        return os.environ["COMPUTERNAME"]
    return socket.gethostname().split(".")[0] or "Computer"


def default_inbox() -> Path:
    """Where received deliveries go unless the user chose something else."""
    home = Path.home()
    if IS_MAC:
        return home / "Movies" / "File Transferer"
    if IS_WIN:
        return home / "Videos" / "File Transferer"
    return home / "File Transferer"


def reveal(path: Path) -> None:
    """Show `path` selected in Finder / Explorer."""
    try:
        if IS_MAC:
            subprocess.Popen(["open", "-R", str(path)])
        elif IS_WIN:
            # Must be passed as one command line: with an argument list, Python quotes
            # the whole '/select,<path>' when the path contains spaces, which Explorer
            # does not understand (it then opens "Documents" instead).
            subprocess.Popen(f'explorer /select,"{Path(path).resolve()}"')
        else:
            subprocess.Popen(["xdg-open", str(Path(path).parent)])
    except OSError:
        log.exception("Could not reveal %s", path)


def open_path(path: Path) -> None:
    try:
        if IS_MAC:
            subprocess.Popen(["open", str(path)])
        elif IS_WIN:
            os.startfile(str(path))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        log.exception("Could not open %s", path)


def open_google_drive() -> None:
    """Start Google Drive for desktop (macOS; on Windows there is no stable launcher path)."""
    if not IS_MAC:
        return
    try:
        subprocess.Popen(["open", "-a", "Google Drive"])
    except OSError:
        log.exception("Could not start Google Drive")


def open_privacy_settings() -> None:
    """macOS: Privacy & Security → Files and Folders / Full Disk Access."""
    if IS_MAC:
        try:
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
        except OSError:
            log.exception("Could not open the privacy settings")


def notify(title: str, text: str) -> None:
    if not IS_MAC:
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    try:
        subprocess.Popen(["osascript", "-e",
                          f'display notification "{esc(text)}" with title "{esc(title)}" sound name "Glass"'])
    except OSError:
        pass


class KeepAwake:
    """Prevents idle sleep while the app is working (macOS `caffeinate`)."""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        if not IS_MAC or self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            self._proc = None

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            self._proc = None

    @property
    def active(self) -> bool:
        return self._proc is not None


# ---------------------------------------------------------------- start at login (macOS)

LOGIN_LABEL = "app.file-transferer.login"
DEFAULT_APP = Path("/Applications/file-transferer.app")


def app_bundle() -> Optional[Path]:
    """The running .app bundle (…/file-transferer.app), if started from one."""
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False) and len(exe.parents) > 2 and exe.parents[2].suffix == ".app":
        return exe.parents[2]
    return None


def login_item_path(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LOGIN_LABEL}.plist"


def login_item_plist(app: Path) -> bytes:
    """A LaunchAgent that opens the app once when the user logs in."""
    return plistlib.dumps({
        "Label": LOGIN_LABEL,
        "ProgramArguments": ["/usr/bin/open", "-a", str(app)],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    })


def launch_at_login_enabled(home: Optional[Path] = None) -> bool:
    return login_item_path(home).exists()


def set_launch_at_login(enabled: bool, app: Optional[Path] = None, home: Optional[Path] = None) -> None:
    """Install or remove the LaunchAgent. It takes effect at the next login, so the
    app is not started a second time now."""
    path = login_item_path(home)
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(login_item_plist(app or app_bundle() or DEFAULT_APP))
        log.info("Start at login enabled (%s)", path)
        return
    if path.exists():
        path.unlink()
        log.info("Start at login disabled")
    if IS_MAC and home is None:
        # Unload it if it was loaded at login; harmless if it was not
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LOGIN_LABEL}"],
                       capture_output=True, check=False)


def enable_drop(root) -> bool:
    """Load the tkdnd extension so files can be dropped onto the window."""
    try:
        from tkinterdnd2 import TkinterDnD
        version = TkinterDnD._require(root)
        log.info("Drag and drop onto the window enabled (tkdnd %s)", version)
        return True
    except Exception:  # noqa: BLE001 – drop onto the Dock icon still works
        log.info("Drag and drop onto the window is not available", exc_info=True)
        return False
