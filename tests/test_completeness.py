"""Completeness check of a delivery (file names only), stall detection and the diag command."""

import random
import subprocess
from pathlib import Path

import pytest

import file_transferer.diag as diag
from file_transferer import cli, watch
from file_transferer.friendly import Eta, StallTracker, running_text
from file_transferer.manifest import STATUS_IN_PROGRESS, read_manifest, write_manifest
from file_transferer.messages import completeness_report, completeness_summary
from file_transferer.packet import packet_name
from file_transferer.sender import SendOptions, send_folder

K, M = 4, 2
OPTS = SendOptions(k=K, m=M, block_size=4096)


@pytest.fixture
def delivery(tmp_path):
    src = tmp_path / "Shoot"
    src.mkdir()
    (src / "a.mp4").write_bytes(random.Random(1).randbytes(100_000))
    drive = tmp_path / "Drive"
    drive.mkdir()
    return send_folder(src, drive, OPTS).target


def remove(folder: Path, stripe: int, indexes) -> None:
    for i in indexes:
        (folder / packet_name(stripe, i)).unlink()


def test_all_packets_present(delivery):
    check = watch.check_delivery(delivery)
    assert check.complete and check.present == check.expected and check.restorable
    assert check.missing_names == [] and check.more_needed == 0
    assert "All packets are in the cloud folder" in completeness_summary(check)


def test_missing_spare_packets_are_harmless(delivery):
    remove(delivery, 0, [4, 5])                         # parity only
    check = watch.check_delivery(delivery)
    assert check.restorable and check.missing_names == [packet_name(0, 4), packet_name(0, 5)]
    assert "everything can still be restored" in completeness_summary(check)


def test_blocked_stripe_is_reported_with_exact_numbers(delivery):
    stripes = len(watch.check_delivery(delivery).stripes)
    assert stripes > 3
    remove(delivery, 0, [0, 1, 2])                      # 3 of 6 left, 4 needed -> 1 more
    remove(delivery, 2, range(K + M))                   # whole stripe gone -> 4 more
    check = watch.check_delivery(delivery)
    assert not check.restorable
    assert [s.stripe for s in check.blocked] == [0, 2]
    assert [s.short_by for s in check.blocked] == [1, 4] and check.more_needed == 5
    text = completeness_report(check)
    assert "Stripe 1: missing 00, 01, 02 (1 more needed)" in text
    assert "Stripe 3: all 6 packets missing (4 more needed)" in text
    assert packet_name(2, 0) in text and "may have stopped" in text


def test_report_cuts_long_name_lists(delivery):
    remove(delivery, 1, range(K + M))
    check = watch.check_delivery(delivery)
    assert "and 3 more" in completeness_report(check, max_names=3)
    assert "more" not in completeness_report(check, max_names=0).split("Missing packets:")[1]


def test_unfinished_delivery_only_lists_stripes_seen_so_far(delivery):
    man = read_manifest(delivery)
    write_manifest(delivery, {**man, "status": STATUS_IN_PROGRESS})
    remove(delivery, 0, [1])
    check = watch.check_delivery(delivery)
    assert not check.complete and not check.restorable
    assert check.missing_names == [packet_name(0, 1)]
    assert "has not finished uploading yet" in completeness_summary(check)


def test_not_a_delivery(tmp_path):
    assert watch.check_delivery(tmp_path) is None


def test_status_missing_lists_names(delivery, capsys):
    remove(delivery, 0, [2, 3])
    assert cli.main(["status", str(delivery), "--missing"]) == 2
    assert capsys.readouterr().out.split() == [packet_name(0, 2), packet_name(0, 3)]


def test_status_missing_is_silent_when_complete(delivery, capsys):
    assert cli.main(["status", str(delivery), "--missing"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_status_without_manifest(tmp_path, capsys):
    assert cli.main(["status", str(tmp_path), "--missing"]) == 1


def test_status_shows_the_report(delivery, capsys):
    remove(delivery, 0, [0, 1, 2])
    cli.main(["status", str(delivery)])
    out = capsys.readouterr().out
    assert "Stripe 1: missing 00, 01, 02" in out and "--missing" in out


# ---------------------------------------------------------------- stall detection

def test_stall_needs_no_progress_for_the_whole_time():
    t = StallTracker(minutes=15)
    assert t.update("a", 10, now=0) is None
    assert t.update("a", 10, now=14 * 60) is None            # not long enough yet
    assert t.update("a", 10, now=16 * 60) == 16
    assert t.update("a", 11, now=17 * 60) is None            # a new packet arrived: reset
    assert t.update("a", 11, now=31 * 60) is None
    assert t.update("b", 5, now=40 * 60) is None             # other deliveries are independent


def test_stalled_text_on_the_card():
    waiting = running_text("receive", 0, 0, "Waiting", Eta(), 35, 20)
    assert waiting.startswith("Still uploading (35 %)") and "No new packets for 20 min" in waiting
    assert "No new packets" not in running_text("receive", 0, 0, "Waiting", Eta(), 35, None)


# ---------------------------------------------------------------- diag

def test_diag_describes_files_and_handles_other_systems(delivery):
    text = diag.collect(delivery)
    assert "File Transferer diagnostics" in text and "manifest.json" in text
    assert text.count(".vtp ---") >= 1
    assert diag.collect(delivery / "nope").endswith("The path does not exist.\n")


def test_diag_runs_the_mac_tools_and_survives_missing_ones(delivery, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == "mdls":
            raise FileNotFoundError
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{cmd[0]} output", stderr="")

    monkeypatch.setattr(diag, "IS_MAC", True)
    monkeypatch.setattr(diag.subprocess, "run", fake_run)
    text = diag.collect(delivery / "manifest.json")
    assert {"xattr", "ls", "mdls", "fileproviderctl"} <= set(calls)
    assert "xattr output" in text and "(tool not available)" in text


def test_diag_samples_oldest_and_newest_packets(delivery):
    names = [p.name for p in diag.sample_files(delivery)]
    assert names[0] == "manifest.json" and len(names) <= 6 and len(set(names)) == len(names)


def test_diag_command_writes_the_report(delivery, tmp_path, capsys):
    out = tmp_path / "report.txt"
    assert cli.main(["diag", str(delivery), "-o", str(out)]) == 0
    assert "File Transferer diagnostics" in out.read_text(encoding="utf-8")
    assert str(out) in capsys.readouterr().out
