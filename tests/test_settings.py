"""Settings, the main window and its behaviour (dialogs, modes, cards)."""

import sys
from pathlib import Path

import pytest

import file_transferer.desktop as desktop
import file_transferer.gui as gui
from file_transferer.config import CONFIG_DIR_ENV, Config
from file_transferer.history import DONE, FAILED, INCOMPLETE, PAUSED, QUEUED, Delivery, History
from file_transferer.manifest import MANIFEST_NAMES
from conftest import new_root


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "config"))
    monkeypatch.setattr(gui, "find_drive_roots", lambda: [])
    monkeypatch.setattr(desktop, "notify", lambda *a: None)
    monkeypatch.setattr(desktop, "default_inbox", lambda: tmp_path / "Inbox")


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


def make_app(monkeypatch, dialogs, paths=()):
    monkeypatch.setattr(gui.filedialog, "askdirectory", dialogs.askdirectory)
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: None)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    root = new_root()
    a = gui.App(root, list(paths))
    a.submitted = []
    a.submit = a.submitted.append                  # do not actually run jobs
    return a


@pytest.fixture
def app(monkeypatch):
    dialogs = Dialogs()
    a = make_app(monkeypatch, dialogs)
    yield a, dialogs
    a.root.destroy()


def delivery_folder(parent: Path, name: str) -> Path:
    folder = parent / name
    folder.mkdir(parents=True)
    (folder / MANIFEST_NAMES[0]).write_text("{}", encoding="utf-8")
    return folder


# ---------------------------------------------------------------- send target and dialogs

def test_chosen_local_target_is_kept(app, tmp_path):
    a, dialogs = app
    local = tmp_path / "Lokaler Test"
    local.mkdir()
    dialogs.answers = [str(local)]
    assert a._drive_root(ask=True) == local
    assert Config.load().drive_root == str(local)
    assert a._drive_root() == local                # no dialog, no fallback to Drive


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
    assert [j.name for j in a.submitted] == ["Tag 1", "Tag 2"]
    assert all(j.target == str(target) and j.kind == "send" for j in a.submitted)
    assert Config.load().last_send_dir == str(shoots / "Tag 2")

    (shoots / "Tag 2").rmdir()                     # remembered folder is gone
    a._pick_send()
    assert dialogs.initialdirs[2] == str(shoots)   # nearest existing parent


def test_delivery_goes_to_inbox_without_asking(app, tmp_path):
    a, dialogs = app
    folder = delivery_folder(tmp_path / "Drive", "Shoot")
    dialogs.answers = [str(folder), str(folder)]
    a._pick_delivery()
    assert a.submitted[-1].kind == "receive"
    assert a.submitted[-1].target == str(tmp_path / "Inbox")   # default inbox, no destination dialog
    assert (tmp_path / "Inbox").is_dir()
    a.submitted[-1].cancel.set()
    a.history.items[0].state = DONE                 # finished, so it may be received again
    a._pick_delivery()
    assert dialogs.initialdirs[1] == str(folder)    # transfer dialog opens in the last one


# ---------------------------------------------------------------- modes and drops

@pytest.mark.gui
@pytest.mark.skipif(sys.platform == "darwin", reason="needs two Tk roots in one process, which hangs on macOS")
def test_mode_is_remembered(monkeypatch):
    dialogs = Dialogs()
    a = make_app(monkeypatch, dialogs)
    assert a.mode_var.get() == "send"
    a.set_mode("receive")
    a.root.destroy()
    b = make_app(monkeypatch, dialogs)
    assert b.mode_var.get() == "receive" and Config.load().mode == "receive"
    b.root.destroy()


def test_drop_sorts_items(app, tmp_path):
    a, dialogs = app
    target = tmp_path / "target"
    target.mkdir()
    Config.load().update(drive_root=str(target))
    shoot = tmp_path / "Drehtag 3"
    shoot.mkdir()
    f1, f2 = tmp_path / "a.mp4", tmp_path / "b.wav"
    f1.write_bytes(b"1")
    f2.write_bytes(b"2")
    incoming = delivery_folder(tmp_path / "Drive", "Interview")

    a.handle_paths([str(shoot), str(f1), str(f2), str(incoming)])
    kinds = sorted((j.kind, j.name.split(" ")[0], len(j.sources)) for j in a.submitted)
    assert kinds == [("receive", "Interview", 1), ("send", "Drehtag", 1), ("send", "Files", 2)]
    files_job = next(j for j in a.submitted if j.options.get("files"))
    assert ":" not in files_job.name                # valid folder name everywhere
    assert a.mode_var.get() == "send"


def test_same_delivery_is_not_started_twice(app, tmp_path):
    a, dialogs = app
    folder = delivery_folder(tmp_path / "Drive", "Shoot")
    a.handle_paths([str(folder)])
    a.handle_paths([str(folder)])
    assert len(a.submitted) == 1


def test_other_window_change_is_picked_up(app, tmp_path):
    a, dialogs = app
    old, new = tmp_path / "alt", tmp_path / "neu"
    old.mkdir()
    new.mkdir()
    Config.load().update(drive_root=str(old))
    a._reload_config()
    Config.load().update(drive_root=str(new))      # changed in another window
    assert a._drive_root() == new


# ---------------------------------------------------------------- cards

def test_finished_jobs_update_cards(app, tmp_path):
    from types import SimpleNamespace

    a, dialogs = app
    target = tmp_path / "target"
    target.mkdir()
    Config.load().update(drive_root=str(target))
    shoot = tmp_path / "Drehtag 3"
    shoot.mkdir()
    a.handle_paths([str(shoot)])
    job = a.submitted[0]
    d = a.history.get(job.delivery_id)
    assert d.state == QUEUED and a._card(d).button.cget("text") == "Stop"

    a._on_started(job)
    a._on_progress(job, 50, 100, "Stripe 1: computing parity …")
    status = a._card(d).status.cget("text")
    assert status.startswith("Preparing – 50 %") and "Stripe" not in status

    result = SimpleNamespace(target=target / "Drehtag 3", source_bytes=100, files=[{}, {}], transfer_id="t1")
    a._on_finished(job, result, None)
    assert d.state == DONE and a._card(d).status.cget("text").startswith("✔ In the cloud")
    assert not a._card(d).button.winfo_ismapped()


def test_errors_become_plain_language_with_one_action(app, tmp_path):
    from file_transferer.receiver import InsufficientSpaceError, StripeUnavailableError
    from file_transferer.util import Cancelled

    a, dialogs = app
    folder = delivery_folder(tmp_path / "Drive", "Shoot")
    for exc, state, action, words in (
            (InsufficientSpaceError(50 << 30, 15 << 30), FAILED, "choose_inbox", "35.0 GB"),
            (StripeUnavailableError(2, 8, 10, True), FAILED, "salvage", "missing"),
            (Cancelled(), PAUSED, "resume", "Paused")):
        a.history.items.clear()
        a.submitted.clear()
        a.handle_paths([str(folder)])
        job = a.submitted[0]
        a._on_started(job)
        a._on_finished(job, None, exc)
        d = a.history.get(job.delivery_id)
        assert (d.state, d.action) == (state, action) and words in d.message
        assert "Traceback" not in a._card(d).status.cget("text")


def test_incomplete_receive_asks_to_send_again(app, tmp_path):
    from types import SimpleNamespace

    a, dialogs = app
    folder = delivery_folder(tmp_path / "Drive", "Shoot")
    a.handle_paths([str(folder)])
    job = a.submitted[0]
    a._on_started(job)
    res = SimpleNamespace(dest=tmp_path / "Inbox" / "Shoot (incomplete)", sender="Kamera-Mac", bytes=10,
                          files=3, repaired_stripes=0, renamed={}, without_manifest=False,
                          complete=False, missing=["b.mp4", "c.mp4"])
    a._on_finished(job, res, None)
    d = a.history.get(job.delivery_id)
    assert d.state == INCOMPLETE and d.action == "fetch_missing"
    assert "2 files are still missing" in d.message and "send again" in d.message

    a.run_action(d, "fetch_missing")               # after the sender sent again
    assert d.state == QUEUED and a.submitted[-1].options.get("salvage") is None


def test_salvage_action_requeues_with_salvage(app, tmp_path):
    a, dialogs = app
    folder = delivery_folder(tmp_path / "Drive", "Shoot")
    a.handle_paths([str(folder)])
    d = a.history.items[0]
    d.state, d.action = FAILED, "salvage"
    a.run_action(d, "salvage")
    assert a.submitted[-1].options.get("salvage") is True


def test_history_survives_restart(tmp_path):
    h = History(tmp_path / "h.json")
    running = h.add(Delivery("send", "Drehtag 3", ["/x"], "/drive", state="running"))
    h.add(Delivery("receive", "Interview", ["/y"], "/inbox", state=DONE))
    again = History.load(tmp_path / "h.json")
    assert [d.name for d in again.items] == ["Interview", "Drehtag 3"]
    restored = again.get(running.id)
    assert restored.state == PAUSED and restored.action == "resume"


# ---------------------------------------------------------------- platform helpers

@pytest.mark.skipif(not desktop.IS_WIN, reason="Windows Explorer command line")
def test_reveal_quotes_path_with_spaces(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd))
    target = tmp_path / "Dreh Tag 1 (Kamera A)"
    target.mkdir()
    desktop.reveal(target)
    # One command line with only the path quoted – an argument list would quote
    # "/select,<path>" as a whole, and Explorer would open "Documents" instead.
    assert calls == [f'explorer /select,"{target.resolve()}"']


# ---------------------------------------------------------------- completeness check

def make_partial_delivery(tmp_path):
    import random

    from file_transferer.packet import packet_name
    from file_transferer.sender import SendOptions, send_folder

    src = tmp_path / "Shoot"
    src.mkdir()
    (src / "a.mp4").write_bytes(random.Random(1).randbytes(60_000))
    drive = tmp_path / "Cloud"
    drive.mkdir()
    folder = send_folder(src, drive, SendOptions(k=4, m=2, block_size=4096)).target
    for i in range(3):                                   # stripe 1 keeps 3 of the 4 packets it needs
        (folder / packet_name(0, i)).unlink()
    return folder


def test_settings_check_reports_missing_packets(app, tmp_path, monkeypatch):
    a, dialogs = app
    shown = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda title, text, **k: shown.append(("warn", text)))
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, text, **k: shown.append(("info", text)))
    folder = make_partial_delivery(tmp_path)
    a.check_completeness(folder)
    kind, text = shown[-1]
    assert kind == "warn" and "Stripe 1: missing 00, 01, 02 (1 more needed)" in text
    assert "status --missing" in text
    a.check_completeness(tmp_path)                        # not a delivery
    assert "not a readable delivery" in shown[-1][1]


def test_incomplete_card_names_the_missing_packets(app, tmp_path):
    from types import SimpleNamespace

    a, dialogs = app
    folder = make_partial_delivery(tmp_path)
    a.handle_paths([str(folder)])
    job = a.submitted[0]
    a._on_started(job)
    res = SimpleNamespace(dest=tmp_path / "Inbox" / "Shoot (incomplete)", sender=None, bytes=0, files=0,
                          repaired_stripes=0, renamed={}, without_manifest=False,
                          complete=False, missing=["a.mp4"])
    a._on_finished(job, res, None)
    d = a.history.get(job.delivery_id)
    assert d.state == INCOMPLETE
    assert "packets are missing in the cloud folder" in d.hint and "send the folder again" in d.hint
    assert "Stripe 1: missing 00, 01, 02" in d.details
