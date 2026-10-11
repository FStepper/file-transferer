"""Phase 10: watching the Drive folder, automatic receiving, freeing Drive space."""

import random
import sys
import time
import tkinter as tk
from pathlib import Path

import pytest

import file_transferer.desktop as desktop
import file_transferer.gui as gui
import file_transferer.receiver as receiver
from file_transferer import watch
from file_transferer.config import CONFIG_DIR_ENV, Config
from file_transferer.history import DONE, INCOMPLETE, OFFERED, Delivery
from file_transferer.manifest import STATUS_IN_PROGRESS, read_manifest, write_manifest
from file_transferer.packet import packet_name
from file_transferer.sender import SendOptions, send_folder
from conftest import new_root

K, M = 4, 2
OPTS = SendOptions(k=K, m=M, block_size=4096)


def make_source(root: Path, name="Drehtag 3", seed=1) -> Path:
    rnd = random.Random(seed)
    src = root / "src" / name
    src.mkdir(parents=True)
    (src / "a.mp4").write_bytes(rnd.randbytes(40_000))
    (src / "b.mp4").write_bytes(rnd.randbytes(40_000))
    return src


# ---------------------------------------------------------------- scanning

def test_scan_finds_deliveries_and_ignores_the_rest(tmp_path):
    drive = tmp_path / "Drive"
    drive.mkdir()
    res = send_folder(make_source(tmp_path), drive, OPTS, sender="Kamera-Mac")
    (drive / "Privat").mkdir()                     # unrelated folder
    (drive / ".versteckt").mkdir()
    found = watch.scan(drive)
    assert [f.name for f in found] == ["Drehtag 3"]
    f = found[0]
    assert f.complete and f.recoverable and f.percent == 100 and f.sender == "Kamera-Mac"
    assert f.transfer_id == res.transfer_id and f.files == 2


def test_scan_percent_while_uploading_and_lost_stripe(tmp_path):
    drive = tmp_path / "Drive"
    drive.mkdir()
    res = send_folder(make_source(tmp_path), drive, OPTS)
    man = read_manifest(res.target)
    for i in range(K + M):                         # last stripes not uploaded yet
        for s in range(res.stripes - 2, res.stripes):
            (res.target / packet_name(s, i)).unlink()
    write_manifest(res.target, {**man, "status": STATUS_IN_PROGRESS})
    f = watch.inspect(res.target)
    assert not f.complete and not f.recoverable and 0 < f.percent < 100

    write_manifest(res.target, man)                # upload done, but 2 stripes are lost
    f = watch.inspect(res.target)
    assert f.complete and not f.recoverable and f.percent < 100


def test_delete_delivery_only_deletes_own_files(tmp_path):
    drive = tmp_path / "Drive"
    drive.mkdir()
    res = send_folder(make_source(tmp_path), drive, OPTS)
    with pytest.raises(watch.NotADeliveryError):
        watch.delete_delivery(res.target, "0" * 32)            # wrong delivery
    (res.target / "Notizen.txt").write_text("wichtig", encoding="utf-8")
    with pytest.raises(watch.NotADeliveryError):
        watch.delete_delivery(res.target, res.transfer_id)     # foreign file inside
    assert (res.target / "Notizen.txt").exists()
    (res.target / "Notizen.txt").unlink()
    watch.delete_delivery(res.target, res.transfer_id)
    assert not res.target.exists()


# ---------------------------------------------------------------- automatic receiving in the window

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "config"))
    monkeypatch.setattr(desktop, "notify", lambda *a: None)
    monkeypatch.setattr(gui, "find_drive_roots", lambda: [])
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(gui, "SCAN_SECONDS", 0.2)
    drive, inbox = tmp_path / "Drive", tmp_path / "Inbox"
    drive.mkdir()
    Config.load().update(drive_root=str(drive), receive_dir=str(inbox), mode="receive",
                         auto_initialized=True, k=K, m=M, block_mib=1)
    apps = []

    def start():
        root = new_root()
        app = gui.App(root, [])
        apps.append(app)
        return app

    yield drive, inbox, start
    for app in apps:
        try:
            app.root.destroy()
        except tk.TclError:
            pass


def run_until(app, cond, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def receive_cards(app):
    return app.history.of_kind("receive")


@pytest.mark.gui
def test_new_delivery_is_received_automatically(env, tmp_path):
    drive, inbox, start = env
    app = start()
    src = make_source(tmp_path)
    send_folder(src, drive, OPTS, sender="Kamera-Mac")           # sent by another Mac
    assert run_until(app, lambda: receive_cards(app) and receive_cards(app)[0].state == DONE)
    d = receive_cards(app)[0]
    assert d.sender == "Kamera-Mac" and Path(d.result) == inbox / "Drehtag 3"
    assert (Path(d.result) / "a.mp4").read_bytes() == (src / "a.mp4").read_bytes()
    # Found once – not received a second time
    for _ in range(10):
        app.root.update()
        time.sleep(0.05)
    assert len(receive_cards(app)) == 1


@pytest.mark.gui
def test_first_start_offers_existing_deliveries(env, tmp_path):
    drive, inbox, start = env
    Config.load().update(auto_initialized=False)
    send_folder(make_source(tmp_path), drive, OPTS)              # was there before
    app = start()
    assert run_until(app, lambda: receive_cards(app))
    d = receive_cards(app)[0]
    assert d.state == OFFERED and app._card(d).button.cget("text") == "Receive"
    assert not (inbox / "Drehtag 3").exists()
    app.run_action(d, "receive_now")
    assert run_until(app, lambda: d.state == DONE)
    assert len(receive_cards(app)) == 1


@pytest.mark.gui
def test_own_deliveries_are_only_offered(env, tmp_path):
    drive, inbox, start = env
    app = start()
    app.set_mode("send")
    app.handle_paths([str(make_source(tmp_path))])                # sent from this computer
    sent = app.history.of_kind("send")[0]
    assert run_until(app, lambda: sent.state == DONE)
    app.set_mode("receive")
    assert run_until(app, lambda: receive_cards(app))
    d = receive_cards(app)[0]
    assert d.state == OFFERED and "this computer" in d.message


@pytest.mark.gui
@pytest.mark.skipif(sys.platform == "darwin",
                    reason="restarts the app, i.e. a second Tk root in one process, which hangs on macOS")
def test_removed_card_is_not_fetched_again(env, tmp_path):
    drive, inbox, start = env
    app = start()
    send_folder(make_source(tmp_path), drive, OPTS)
    assert run_until(app, lambda: receive_cards(app) and receive_cards(app)[0].state == DONE)
    app.remove(receive_cards(app)[0])
    app.root.destroy()
    again = start()                                               # after a restart
    for _ in range(20):
        again.root.update()
        time.sleep(0.05)
    assert receive_cards(again) == []


@pytest.mark.gui
def test_lost_parts_are_restored_and_completed_later(env, tmp_path, monkeypatch):
    drive, inbox, start = env
    monkeypatch.setattr(receiver, "GIVE_UP_SECONDS", 0)
    monkeypatch.setattr(receiver, "POLL_SECONDS", 0.1)
    src = make_source(tmp_path)
    res = send_folder(src, drive, OPTS)
    man = read_manifest(res.target)
    b = next(f for f in man["files"] if f["path"] == "b.mp4")
    lost = (b["offset"] + b["length"] // 2) // (K * 4096)       # a stripe inside b.mp4
    saved = {}
    for i in range(M + 1):
        p = res.target / packet_name(lost, i)
        saved[p] = p.read_bytes()
        p.unlink()

    app = start()
    assert run_until(app, lambda: receive_cards(app) and receive_cards(app)[0].state == INCOMPLETE)
    d = receive_cards(app)[0]
    assert "1 file is still missing" in d.message and d.action == "fetch_missing"
    assert (Path(d.result) / "a.mp4").exists() and not (Path(d.result) / "b.mp4").exists()

    for p, raw in saved.items():                                  # the sender sent again
        p.write_bytes(raw)
    assert run_until(app, lambda: d.state == DONE)
    assert Path(d.result) == inbox / "Drehtag 3"
    assert (Path(d.result) / "b.mp4").read_bytes() == (src / "b.mp4").read_bytes()


@pytest.mark.gui
def test_free_drive_space_after_arrival(env, tmp_path):
    drive, inbox, start = env
    app = start()
    res = send_folder(make_source(tmp_path), drive, OPTS)
    assert run_until(app, lambda: receive_cards(app) and receive_cards(app)[0].state == DONE)
    d = receive_cards(app)[0]
    card = app._card(d)
    assert card.link.winfo_manager() and "Free up cloud space" in card.link.cget("text")
    app.free_drive(d)
    assert not res.target.exists() and (inbox / "Drehtag 3" / "a.mp4").exists()
    assert d.hint == gui.DELETED_HINT and not app.can_free_drive(d)


def test_replace_retries_while_destination_is_busy(tmp_path, monkeypatch):
    """Windows: replacing a file that is open elsewhere (Drive client, a reader) fails briefly."""
    import os
    from file_transferer import util

    real, calls = os.replace, []

    def busy_twice(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError("in use")
        real(src, dst)

    monkeypatch.setattr(util.os, "replace", busy_twice)
    util.atomic_write_bytes(tmp_path / "manifest.json", b"{}")
    assert (tmp_path / "manifest.json").read_bytes() == b"{}" and len(calls) == 3


def test_waiting_card_shows_incoming_percent():
    from file_transferer.friendly import Eta, running_text

    assert running_text("receive", 0, 0, "Waiting for stripe 3", Eta(), 35).startswith(
        "Still uploading (35 %)")
    assert running_text("receive", 0, 0, "Waiting for stripe 3", Eta(), None).startswith("Waiting for the cloud")
