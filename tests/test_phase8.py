"""Phase 8: selective restore, partial recovery, emergency without manifest, portable names."""

import random
import unicodedata
from pathlib import Path

import pytest

import file_transferer.prefetch as prefetch
import file_transferer.receiver as receiver
from file_transferer.manifest import MANIFEST_NAMES, read_manifest
from file_transferer.names import IS_WINDOWS, NameMapper
from file_transferer.packet import packet_name
from file_transferer.receiver import (
    INCOMPLETE_SUFFIX, REPORT_NAME, STATE_NAME, StripeUnavailableError, TransferError,
    receive_folder, verify_transfer,
)
from file_transferer.recovery import list_transfer, synthesize_manifest
from file_transferer.sender import SendOptions, send_folder

K, M = 4, 2
STRIPE = K * 4096
OPTS = SendOptions(k=K, m=M, block_size=4096, zstd_level=3)


@pytest.fixture
def shoot(tmp_path):
    rnd = random.Random(3)
    src = tmp_path / "Shoot"
    (src / "Audio").mkdir(parents=True)
    (src / "Leer").mkdir()
    for n in ("a.mp4", "b.mp4", "c.mp4"):
        (src / n).write_bytes(rnd.randbytes(60_000))                  # ~4 stripes each
    (src / "Audio" / "ton1.wav").write_bytes(bytes(range(256)) * 200)
    (src / "Audio" / "ton2.wav").write_bytes(bytes(range(255, -1, -1)) * 200)
    drive = tmp_path / "My Drive"
    drive.mkdir()
    res = send_folder(src, drive, OPTS)
    return src, res, tmp_path / "out"


def files_of(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.name not in (REPORT_NAME, STATE_NAME)}


def stripes_of(man: dict, path: str) -> range:
    f = next(f for f in man["files"] if f["path"] == path)
    return range(f["offset"] // STRIPE, (f["offset"] + f["length"] - 1) // STRIPE + 1)


def lose_stripe(folder: Path, s: int) -> None:
    for i in range(M + 1):
        (folder / packet_name(s, i)).unlink()


# ---------------------------------------------------------------- selective restore

def test_only_restores_matching_files_and_loads_only_their_stripes(shoot, monkeypatch):
    src, res, dest = shoot
    man = read_manifest(res.target)
    real = prefetch.check_packet
    seen = set()

    def spy(folder, man_, s, i, *a, **kw):
        seen.add(s)
        return real(folder, man_, s, i, *a, **kw)

    monkeypatch.setattr(prefetch, "check_packet", spy)
    out = receive_folder(res.target, dest, wait=False, only=["Audio/*.wav"])
    assert out.selected and out.complete and out.files == 2
    assert files_of(out.dest) == {k: v for k, v in files_of(src).items() if k.startswith("Audio/")}
    wanted = set(stripes_of(man, "Audio/ton1.wav")) | set(stripes_of(man, "Audio/ton2.wav"))
    assert seen <= wanted | {max(wanted) + 1, max(wanted) + 2}     # plus look-ahead
    assert not (out.dest / "Leer").exists()                         # no unrelated folders


def test_only_by_file_name_and_folder(shoot):
    src, res, dest = shoot
    out = receive_folder(res.target, dest, wait=False, only=["B.MP4", "Audio/"])
    assert sorted(files_of(out.dest)) == ["Audio/ton1.wav", "Audio/ton2.wav", "b.mp4"]


def test_only_without_match_fails(shoot):
    src, res, dest = shoot
    with pytest.raises(TransferError):
        receive_folder(res.target, dest, wait=False, only=["*.mov"])


def test_only_and_salvage_need_format_2(tmp_path):
    src = tmp_path / "S"
    src.mkdir()
    (src / "a.mp4").write_bytes(b"x" * 5000)
    (tmp_path / "D").mkdir()
    res = send_folder(src, tmp_path / "D", SendOptions(k=K, m=M, block_size=4096, format_version=1))
    with pytest.raises(TransferError):
        receive_folder(res.target, tmp_path / "out", wait=False, only=["a.mp4"])


# ---------------------------------------------------------------- partial recovery

def test_salvage_restores_everything_else_and_can_be_completed(shoot):
    src, res, dest = shoot
    man = read_manifest(res.target)
    b = stripes_of(man, "b.mp4")
    lost = b[len(b) // 2]                                            # a stripe only b.mp4 uses
    assert lost not in stripes_of(man, "a.mp4") and lost not in stripes_of(man, "c.mp4")
    saved = {packet_name(lost, i): (res.target / packet_name(lost, i)).read_bytes() for i in range(M + 1)}
    lose_stripe(res.target, lost)

    with pytest.raises(StripeUnavailableError) as info:
        receive_folder(res.target, dest, wait=False)
    assert info.value.stripe == lost and info.value.upload_complete

    out = receive_folder(res.target, dest, wait=False, salvage=True)
    assert not out.complete and out.missing == ["b.mp4"] and out.lost_stripes == [lost]
    assert out.dest.name == src.name + INCOMPLETE_SUFFIX
    assert (out.dest / STATE_NAME).exists()                         # needed to complete it later
    expected = files_of(src)
    expected.pop("b.mp4")
    assert files_of(out.dest) == expected
    report = (out.dest / REPORT_NAME).read_text(encoding="utf-8")
    assert "INCOMPLETE" in report and "b.mp4" in report

    # The sender re-sends (here: the packets come back), then receive into the same destination
    for name, raw in saved.items():
        (res.target / name).write_bytes(raw)
    done = receive_folder(res.target, dest, wait=False)
    assert done.complete and done.resumed and done.dest == dest / src.name
    assert files_of(done.dest) == files_of(src)
    assert not (dest / (src.name + INCOMPLETE_SUFFIX)).exists()
    assert not (done.dest / STATE_NAME).exists()


def test_wait_mode_gives_up_after_a_while(shoot, monkeypatch):
    src, res, dest = shoot
    lose_stripe(res.target, 1)
    monkeypatch.setattr(receiver, "GIVE_UP_SECONDS", 0)
    monkeypatch.setattr(receiver, "sleep_cancellable", lambda s, c: None)
    with pytest.raises(StripeUnavailableError):
        receive_folder(res.target, dest, wait=True)


def test_list_shows_unavailable_files(shoot):
    src, res, dest = shoot
    man = read_manifest(res.target)
    lose_stripe(res.target, stripes_of(man, "c.mp4")[1])
    _, files = list_transfer(res.target)
    state = {f.path: f.available for f in files}
    assert state["c.mp4"] is False and state["a.mp4"] is True and len(files) == 5


# ---------------------------------------------------------------- emergency without manifest

def remove_manifests(folder: Path) -> None:
    for n in MANIFEST_NAMES:
        (folder / n).unlink()


def test_synthesized_manifest_matches(shoot):
    src, res, dest = shoot
    man = read_manifest(res.target)
    remove_manifests(res.target)
    syn = synthesize_manifest(res.target)
    assert syn["transfer_id"] == man["transfer_id"] and syn["stripes"] == man["stripes"]
    assert syn["params"]["block_size"] == man["params"]["block_size"]
    assert syn["format_version"] == 2


def test_receive_without_manifest(shoot):
    src, res, dest = shoot
    remove_manifests(res.target)
    with pytest.raises(TransferError):
        receive_folder(res.target, dest, wait=False)
    out = receive_folder(res.target, dest, wait=False, without_manifest=True)
    assert out.without_manifest and out.complete
    assert files_of(out.dest) == files_of(src)
    assert (out.dest / "Leer").is_dir()
    assert verify_transfer(res.target, without_manifest=True).content_verified


def test_without_manifest_resyncs_after_lost_stripe(shoot):
    src, res, dest = shoot
    man = read_manifest(res.target)
    b = stripes_of(man, "b.mp4")
    lose_stripe(res.target, b[len(b) // 2])
    remove_manifests(res.target)
    out = receive_folder(res.target, dest, wait=False, without_manifest=True)
    assert not out.complete
    got = files_of(out.dest)
    expected = files_of(src)
    assert "b.mp4" not in got
    for name in ("a.mp4", "c.mp4"):                   # c comes after the damage: found via resync
        assert got[name] == expected[name]


# ---------------------------------------------------------------- portable names

def test_name_mapper_windows_rules():
    nm = NameMapper(windows=True, case_insensitive=True)
    assert nm.map("Szene: 1?.mp4") == "Szene_ 1_.mp4"
    assert nm.map("CON.wav") == "_CON.wav"
    assert nm.map("Ende. ") == "Ende"
    assert nm.map("A/x.wav") == "A/x.wav"
    assert nm.map("a/y.wav") == "a (2)/y.wav"        # distinct source folders stay distinct
    assert nm.map("a/z.wav") == "a (2)/z.wav"
    assert nm.map("X.WAV") == "X.WAV"
    assert nm.map("x.wav") == "x (2).wav"            # would overwrite X.WAV
    assert nm.map("../x") is None


def test_name_mapper_is_stable_when_resumed():
    first = NameMapper(windows=True, case_insensitive=True)
    first.map("X.WAV")
    first.map("x.wav")
    again = NameMapper(first.mapping, windows=True, case_insensitive=True)
    assert again.map("x.wav") == "x (2).wav" and again.map("X.WAV") == "X.WAV"


def test_nfc_everywhere(tmp_path):
    nfd = unicodedata.normalize("NFD", "Kämera Ü.wav")
    src = tmp_path / "S"
    src.mkdir()
    (src / nfd).write_bytes(b"audio" * 1000)
    (tmp_path / "D").mkdir()
    res = send_folder(src, tmp_path / "D", OPTS)
    assert read_manifest(res.target)["files"][0]["path"] == unicodedata.normalize("NFC", nfd)
    out = receive_folder(res.target, tmp_path / "out", wait=False)
    names = [p.name for p in out.dest.iterdir() if p.name != REPORT_NAME]
    assert [unicodedata.normalize("NFC", n) for n in names] == ["Kämera Ü.wav"]


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows path limit")
def test_very_long_destination_paths_on_windows(tmp_path, monkeypatch):
    import file_transferer.sender as sender

    src = tmp_path / "S"
    src.mkdir()
    (tmp_path / "D").mkdir()
    (src / "clip.mp4").write_bytes(b"L" * 2000)
    deep = "/".join(["Sehr langer Ordnername für einen Drehtag " + str(i) for i in range(7)]) + "/clip.mp4"
    real_rel = sender._rel
    monkeypatch.setattr(sender, "_rel", lambda p, s: deep if real_rel(p, s) == "clip.mp4" else real_rel(p, s))
    res = send_folder(src, tmp_path / "D", OPTS)
    out = receive_folder(res.target, tmp_path / "out", wait=False)
    target = Path("\\\\?\\" + str((out.dest / deep).resolve()))
    assert len(str(out.dest / deep)) > 260
    assert target.read_bytes() == b"L" * 2000


def test_colon_in_top_level_name_is_restored(tmp_path, monkeypatch):
    """A top-level name with ':' (allowed on macOS) used to be skipped as unsafe."""
    import file_transferer.names as names
    import file_transferer.sender as sender

    src = tmp_path / "S"
    src.mkdir()
    (tmp_path / "D").mkdir()
    (src / "Szene 1.mp4").write_bytes(b"v" * 3000)
    real_rel = sender._rel
    # Simulate a Mac sender whose file is really called "Szene: 1.mp4"
    monkeypatch.setattr(sender, "_rel", lambda p, s: real_rel(p, s).replace("Szene 1", "Szene: 1"))
    res = send_folder(src, tmp_path / "D", OPTS)
    assert read_manifest(res.target)["files"][0]["path"] == "Szene: 1.mp4"

    out = receive_folder(res.target, tmp_path / "out", wait=False)
    expected = "Szene_ 1.mp4" if names.IS_WINDOWS else "Szene: 1.mp4"
    assert (out.dest / expected).read_bytes() == b"v" * 3000
    assert out.renamed == ({"Szene: 1.mp4": expected} if names.IS_WINDOWS else {})
    if names.IS_WINDOWS:
        assert "Szene: 1.mp4  ->  Szene_ 1.mp4" in (out.dest / REPORT_NAME).read_text(encoding="utf-8")
