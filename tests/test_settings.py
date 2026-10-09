"""Settings are merged on save, and folder dialogs remember where they were."""

import tkinter as tk
from pathlib import Path

import pytest

import vidtransfer.gui as gui
from vidtransfer.config import CONFIG_DIR_ENV, Config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "config"))
    monkeypatch.setattr(gui, "find_drive_roots", lambda: [])
    monkeypatch.setattr(gui, "_keep_awake", lambda: None)


def test_update_merges_with_file():
    a, b = Config.load(), Config.load()            # two app windows
    a.update(drive_root="X:/local-test")
    b.update(receive_dir="Y:/export")              # b still has the old drive_root in memory
    fresh = Config.load()
    assert fresh.drive_root == "X:/local-test" and fresh.receive_dir == "Y:/export"
    assert b.drive_root == "X:/local-test"         # b is refreshed as well


def test_update_rejects_unknown_keys():
    with pytest.raises(KeyError):
        Config.load().update(drvie_root="typo")


class Dialogs:
    """Stand-in for tkinter.filedialog.askdirectory that records the start folder."""

    def __init__(self):
        self.answers: list = []
        self.initialdirs: list = []

    def askdirectory(self, **kw):
        self.initialdirs.append(kw.get("initialdir"))
        return self.answers.pop(0)


@pytest.fixture
def app(monkeypatch):
    dialogs = Dialogs()
    monkeypatch.setattr(gui.filedialog, "askdirectory", dialogs.askdirectory)
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: None)
    root = tk.Tk()
    root.withdraw()
    a = gui.App(root, [])
    a.queued = []
    a.jobs.put = a.queued.append                   # do not actually run jobs
    yield a, dialogs
    root.destroy()


def test_chosen_local_target_is_kept(app, tmp_path):
    a, dialogs = app
    local = tmp_path / "Lokaler Test"
    local.mkdir()
    dialogs.answers = [str(local)]
    assert a._drive_root(ask=True) == local
    assert Config.load().drive_root == str(local)
    assert a._drive_root() == local                # no dialog, no fallback to Drive
    assert "eigener Ordner" in a.drive_var.get()


def test_change_dialog_starts_at_current_target(app, tmp_path):
    a, dialogs = app
    first, second = tmp_path / "eins", tmp_path / "zwei"
    first.mkdir()
    second.mkdir()
    dialogs.answers = [str(first), str(second)]
    a._drive_root(ask=True)
    a._drive_root(ask=True)
    assert dialogs.initialdirs[1] == str(first)


def test_send_dialog_remembers_location(app, tmp_path):
    a, dialogs = app
    target = tmp_path / "target"
    shoots = tmp_path / "Shoots"
    for d in (target, shoots / "Tag 1", shoots / "Tag 2"):
        d.mkdir(parents=True)
    Config.load().update(drive_root=str(target))

    dialogs.answers = [str(shoots / "Tag 1"), str(shoots / "Tag 2"), str(shoots / "Tag 2")]
    a._pick_send()
    a._pick_send()
    assert dialogs.initialdirs[1] == str(shoots / "Tag 1")   # opens in the folder opened last
    assert [j[1].name for j in a.queued] == ["Tag 1", "Tag 2"]
    assert all(j[2] == target for j in a.queued)
    assert Config.load().last_send_dir == str(shoots / "Tag 2")

    (shoots / "Tag 2").rmdir()                     # remembered folder is gone
    a._pick_send()
    assert dialogs.initialdirs[2] == str(shoots)   # nearest existing parent


def test_receive_dialogs_remember_location(app, tmp_path):
    a, dialogs = app
    from vidtransfer.manifest import MANIFEST_NAMES

    transfers = tmp_path / "Drive-Test"
    folder = transfers / "Shoot"
    folder.mkdir(parents=True)
    (folder / MANIFEST_NAMES[0]).write_text("{}", encoding="utf-8")
    export = tmp_path / "export"
    export.mkdir()

    dialogs.answers = [str(folder), str(export), str(folder), str(export)]
    a._pick_receive()
    a._pick_receive()
    # 2nd transfer dialog opens in the last transfer folder, 2nd destination dialog in the last destination
    assert dialogs.initialdirs[2] == str(folder)
    assert dialogs.initialdirs[3] == str(export)
    assert a.queued[-1] == ("receive", folder, export)


@pytest.mark.skipif(not gui.IS_WIN, reason="Windows Explorer command line")
def test_reveal_quotes_path_with_spaces(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gui.subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd))
    target = tmp_path / "Dreh Tag 1 (Kamera A)"
    target.mkdir()
    gui._reveal(target)
    # One command line with only the path quoted – an argument list would quote
    # "/select,<path>" as a whole, and Explorer would open "Documents" instead.
    assert calls == [f'explorer /select,"{target.resolve()}"']


def test_other_window_change_is_picked_up(app, tmp_path):
    a, dialogs = app
    old, new = tmp_path / "alt", tmp_path / "neu"
    old.mkdir()
    new.mkdir()
    Config.load().update(drive_root=str(old))
    a._reload_config()
    Config.load().update(drive_root=str(new))      # changed in another window
    assert a._drive_root() == new
