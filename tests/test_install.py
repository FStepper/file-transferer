"""Phase 11: `file-transferer config`, start at login, and the install script (dry run)."""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import file_transferer.desktop as desktop
from file_transferer.cli import main
from file_transferer.config import CONFIG_DIR_ENV, Config

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "packaging" / "Install File Transferer.command"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "config"))


# ---------------------------------------------------------------- config command

def test_config_set_and_get(tmp_path, capsys):
    drive = tmp_path / "Meine Ablage" / "File Transferer"
    drive.mkdir(parents=True)
    assert main(["config", "--drive", str(drive), "--sender", "Kamera-Mac", "--mode", "receive",
                 "--inbox", str(tmp_path / "Eingang")]) == 0
    cfg = Config.load()
    assert (cfg.drive_root, cfg.sender_name, cfg.mode) == (str(drive), "Kamera-Mac", "receive")
    assert (tmp_path / "Eingang").is_dir()
    capsys.readouterr()
    main(["config", "--get", "sender"])
    assert capsys.readouterr().out.strip() == "Kamera-Mac"
    main(["config", "--sender", ""])                     # empty = back to the computer name
    main(["config", "--get", "sender"])
    assert capsys.readouterr().out.strip() == desktop.computer_name()


def test_config_rejects_missing_drive_folder(tmp_path):
    assert main(["config", "--drive", str(tmp_path / "gibt es nicht")]) == 1
    assert Config.load().drive_root is None


def test_config_lists_drives(monkeypatch, capsys, tmp_path):
    import file_transferer.drive as drive

    monkeypatch.setattr(drive, "find_drive_locations", lambda: [tmp_path / "My Drive", tmp_path / "Team"])
    main(["config", "--list-drives"])
    assert capsys.readouterr().out.split("\n")[:2] == [(tmp_path / "My Drive").as_posix(),
                                                       (tmp_path / "Team").as_posix()]


def test_shared_drives_are_found(tmp_path, monkeypatch):
    import file_transferer.drive as drive

    (tmp_path / "Account" / "My Drive").mkdir(parents=True)
    (tmp_path / "Account" / "Shared drives" / "Produktion").mkdir(parents=True)
    monkeypatch.setattr(drive, "find_drive_roots", lambda: [tmp_path / "Account" / "My Drive"])
    assert drive.find_drive_locations() == [tmp_path / "Account" / "My Drive",
                                            tmp_path / "Account" / "Shared drives" / "Produktion"]


# ---------------------------------------------------------------- start at login

def test_login_item(tmp_path):
    app = Path("/Applications/file-transferer.app")
    assert not desktop.launch_at_login_enabled(tmp_path)
    desktop.set_launch_at_login(True, app=app, home=tmp_path)
    plist = plistlib.loads(desktop.login_item_path(tmp_path).read_bytes())
    assert plist["Label"] == desktop.LOGIN_LABEL and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == ["/usr/bin/open", "-a", str(app)]
    assert desktop.launch_at_login_enabled(tmp_path)
    desktop.set_launch_at_login(False, home=tmp_path)
    assert not desktop.launch_at_login_enabled(tmp_path)


# ---------------------------------------------------------------- install script (dry run)

def find_bash() -> str | None:
    if sys.platform != "win32":
        return "/bin/bash"
    git = shutil.which("git")                       # Git for Windows ships bash; avoid the WSL launcher
    if git:
        for candidate in (Path(git).parents[1] / "usr" / "bin" / "bash.exe",
                          Path(git).parents[1] / "bin" / "bash.exe"):
            if candidate.exists():
                return str(candidate)
    return None


BASH = find_bash()
if " " in sys.executable:                           # VT_APP_BIN is split on spaces by the script
    BASH = None


def run_installer(tmp_path: Path, answers: list[str], home: Path) -> subprocess.CompletedProcess:
    (tmp_path / "pkg" / "file-transferer.app").mkdir(parents=True)
    (tmp_path / "Applications").mkdir(exist_ok=True)
    # Call Python directly: a plain "bash" inside the script could resolve to the
    # WSL launcher on Windows when bash is started from outside Git Bash.
    # Git Bash started from outside lacks its own tools (dirname, …) on PATH
    path = os.pathsep.join([str(Path(BASH).parent), os.environ.get("PATH", "")])
    env = dict(os.environ, PATH=path,
               VT_DRY_RUN="1",
               VT_SRC_APP=(tmp_path / "pkg" / "file-transferer.app").as_posix(),
               VT_DEST_DIR=(tmp_path / "Applications").as_posix(),
               VT_APP_BIN=f"{Path(sys.executable).as_posix()} -m file_transferer",
               HOME=str(home), USERPROFILE=str(home))
    # Bytes, not text: text mode would turn "\n" into "\r\n" on Windows
    out = subprocess.run([BASH, str(SCRIPT)], input=("\n".join(answers) + "\n").encode("utf-8"),
                         capture_output=True, env=env, timeout=60)
    return subprocess.CompletedProcess(out.args, out.returncode, out.stdout.decode("utf-8", "replace"),
                                       out.stderr.decode("utf-8", "replace"))


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_installer_with_typed_folder(tmp_path):
    drive = tmp_path / "Geteilt" / "File Transferer"
    drive.mkdir(parents=True)
    out = run_installer(tmp_path, [drive.as_posix(), "Kamera-Mac", "r", "", "y", "n", ""], tmp_path / "home")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "[dry run] ditto" in out.stdout and "com.apple.quarantine" in out.stdout
    cfg = Config.load()
    assert Path(cfg.drive_root) == drive
    assert (cfg.sender_name, cfg.mode) == ("Kamera-Mac", "receive")
    assert "File Transferer is installed" in out.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_installer_with_listed_folder_and_subfolder(tmp_path):
    home = tmp_path / "home"
    (home / "My Drive").mkdir(parents=True)          # mirror-mode Drive folder in the home folder
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    listed = subprocess.run([sys.executable, "-m", "file_transferer", "config", "--list-drives"],
                            capture_output=True, text=True, env=env).stdout.splitlines()
    number = [Path(p) for p in listed].index(home / "My Drive") + 1
    out = run_installer(tmp_path, [str(number), "", "Ton-Mac", "s", "n", "n", ""], home)
    assert out.returncode == 0, out.stdout + out.stderr
    cfg = Config.load()
    assert Path(cfg.drive_root) == home / "My Drive" / "File Transferer" and Path(cfg.drive_root).is_dir()
    assert (cfg.sender_name, cfg.mode) == ("Ton-Mac", "send")


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_installer_stops_at_end_of_input(tmp_path):
    out = run_installer(tmp_path, [(tmp_path / "falsch").as_posix()], tmp_path / "home")
    assert out.returncode == 1 and "cancelled" in out.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_installer_asks_again_for_a_missing_folder(tmp_path):
    good = tmp_path / "Drive"
    good.mkdir()
    out = run_installer(tmp_path, [(tmp_path / "falsch").as_posix(), good.as_posix(), "", "s", "n", "n", ""],
                        tmp_path / "home")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "This folder does not exist" in out.stdout
    assert Path(Config.load().drive_root) == good
