"""Phase 9: loose files, sender name, and a real send → receive run through the window."""

import time
import tkinter as tk
from pathlib import Path

import pytest

import vidtransfer.desktop as desktop
import vidtransfer.gui as gui
from vidtransfer.config import CONFIG_DIR_ENV, Config
from vidtransfer.friendly import ACTIONS
from vidtransfer.history import DONE
from vidtransfer.manifest import read_manifest
from vidtransfer.receiver import receive_folder
from vidtransfer.sender import SendOptions, SourceMissingError, send_files

OPTS = SendOptions(k=4, m=2, block_size=4096)


def test_send_loose_files_as_one_delivery(tmp_path):
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    one, two, three = tmp_path / "A" / "clip.mp4", tmp_path / "B" / "clip.mp4", tmp_path / "ton.wav"
    one.write_bytes(b"1" * 5000)
    two.write_bytes(b"2" * 5000)                     # same name, other folder
    three.write_bytes(b"3" * 3000)
    drive = tmp_path / "Drive"
    drive.mkdir()
    res = send_files([one, two, three], "Dateien 2026-10-09 14.02", drive, OPTS, sender="Kamera-Mac")
    man = read_manifest(res.target)
    assert man["name"] == "Dateien 2026-10-09 14.02" and man["sender"] == "Kamera-Mac"
    assert sorted(f["path"] for f in man["files"]) == ["clip (2).mp4", "clip.mp4", "ton.wav"]
    out = receive_folder(res.target, tmp_path / "out", wait=False)
    assert out.sender == "Kamera-Mac"
    assert {p.name: p.read_bytes()[:1] for p in out.dest.iterdir() if p.suffix != ".txt"} == \
        {"clip.mp4": b"1", "clip (2).mp4": b"2", "ton.wav": b"3"}


def test_send_missing_file_is_reported(tmp_path):
    (tmp_path / "Drive").mkdir()
    with pytest.raises(SourceMissingError):
        send_files([tmp_path / "gone.mp4"], "x", tmp_path / "Drive", OPTS)


# ---------------------------------------------------------------- real run through the window

def wait_for(root, cond, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_window_sends_and_receives(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "config"))
    monkeypatch.setattr(desktop, "notify", lambda *a: None)
    monkeypatch.setattr(gui, "find_drive_roots", lambda: [])
    drive, inbox = tmp_path / "Drive", tmp_path / "Inbox"
    drive.mkdir()
    Config.load().update(drive_root=str(drive), receive_dir=str(inbox), sender_name="Kamera-Mac",
                         k=4, m=2, block_mib=1)
    src = tmp_path / "Drehtag 3"
    src.mkdir()
    (src / "clip.mp4").write_bytes(bytes(range(256)) * 4000)

    root = tk.Tk()
    root.withdraw()
    app = gui.App(root, [])
    try:
        app.handle_paths([str(src)])
        sent = app.history.of_kind("send")[0]
        assert wait_for(root, lambda: sent.state == DONE), sent.message
        assert Path(sent.result) == drive / "Drehtag 3"
        assert "Liegt in Google Drive" in app._card(sent).status.cget("text")

        app.handle_paths([sent.result])                 # the receiving side drops the delivery
        assert app.mode_var.get() == "receive"
        got = app.history.of_kind("receive")[0]
        assert got.sender == "Kamera-Mac"
        assert wait_for(root, lambda: got.state == DONE), got.message
        assert (Path(got.result) / "clip.mp4").read_bytes() == (src / "clip.mp4").read_bytes()
        assert Path(got.result).parent == inbox
        assert app._card(got).button.cget("text") == ACTIONS["reveal"]   # Finder / Explorer
        assert "von Kamera-Mac" in app._card(got).meta.cget("text")
    finally:
        root.destroy()
