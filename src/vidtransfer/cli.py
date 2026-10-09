"""Command line interface. Without arguments (or with paths only) the GUI starts."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from vidtransfer import __version__
from vidtransfer.config import Config, setup_logging
from vidtransfer.drive import find_drive_roots
from vidtransfer.util import Cancelled, human_bytes

COMMANDS = {"send", "receive", "verify", "status", "list", "config", "gui"}


def _console_progress():
    last = [""]

    def progress(done: int, total: int, msg: str) -> None:
        pct = f"{100 * done / total:5.1f} % " if total else ""
        line = f"\r{pct}{msg}"[:110]
        sys.stdout.write(line.ljust(len(last[0])))
        sys.stdout.flush()
        last[0] = line

    return progress


def _resolve_drive(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    cfg = Config.load()
    if cfg.drive_root and Path(cfg.drive_root).is_dir():
        return Path(cfg.drive_root)
    roots = find_drive_roots()
    if len(roots) == 1:
        return roots[0]
    if not roots:
        raise SystemExit("Google-Drive-Ordner nicht gefunden – bitte mit --drive angeben.")
    raise SystemExit("Mehrere Drive-Ordner gefunden, bitte mit --drive wählen:\n  "
                     + "\n  ".join(map(str, roots)))


def cmd_send(a) -> int:
    from vidtransfer.sender import MIB, send_folder

    opts = Config.load().send_options()
    for name in ("k", "m", "zstd_level"):
        if getattr(a, name) is not None:
            setattr(opts, name, getattr(a, name))
    if a.block_mib:
        opts.block_size = a.block_mib * MIB
    res = send_folder(Path(a.source), _resolve_drive(a.drive), opts, _console_progress())
    print(f"\nZiel: {res.target}\n{human_bytes(res.source_bytes)} → {human_bytes(res.stream_bytes)} "
          f"komprimiert, {res.stripes} Stripes, {res.packets_written} Pakete geschrieben, "
          f"{res.packets_skipped} übersprungen")
    return 0


def cmd_receive(a) -> int:
    from vidtransfer.messages import receive_summary
    from vidtransfer.receiver import StripeUnavailableError, receive_folder

    try:
        res = receive_folder(Path(a.folder), Path(a.dest), _console_progress(), wait=not a.no_wait,
                             only=a.only, salvage=a.salvage, without_manifest=a.without_manifest)
    except StripeUnavailableError as exc:
        print(f"\nFehler: {exc}", file=sys.stderr)
        if exc.upload_complete:
            print("Teile des Transfers fehlen endgültig. Mit --salvage wird alles andere "
                  "wiederhergestellt.", file=sys.stderr)
        return 2
    print("\n" + receive_summary(res))
    return 0 if res.complete else 2


def cmd_verify(a) -> int:
    from vidtransfer.messages import verify_summary
    from vidtransfer.receiver import verify_transfer

    res = verify_transfer(Path(a.folder), _console_progress(), deep=not a.quick,
                          without_manifest=a.without_manifest)
    print("\n" + verify_summary(res))
    return 0 if res.ok or (a.quick and res.complete and not res.unrecoverable) else 2


def cmd_list(a) -> int:
    from vidtransfer.recovery import list_transfer

    man, files = list_transfer(Path(a.folder))
    if not man:
        print("Kein gültiges Manifest gefunden.")
        return 1
    state = {True: "ok", False: "FEHLT", None: "?"}
    for f in files:
        print(f"{state[f.available]:>5}  {human_bytes(f.size):>10}  {f.compression:>4}  {f.path}")
    missing = sum(f.available is False for f in files)
    print(f"{len(files)} Dateien, {human_bytes(sum(f.size for f in files))}"
          + (f", {missing} derzeit nicht wiederherstellbar" if missing else ""))
    return 0 if not missing else 2


def cmd_status(a) -> int:
    from vidtransfer.receiver import transfer_status

    man, stripes = transfer_status(Path(a.folder))
    if not man:
        print("Kein gültiges Manifest gefunden.")
        return 1
    p = man["params"]
    print(f"{man['name']}  [{man['status']}]  k={p['k']} m={p['m']} "
          f"Paket={human_bytes(p['block_size'])}  Quelle={human_bytes(man['source_bytes'])}")
    bad = [s for s in stripes if not s.recoverable]
    full = sum(s.present == s.total for s in stripes)
    print(f"{len(stripes)} Stripes: {full} vollständig, {len(stripes) - full - len(bad)} reparierbar, "
          f"{len(bad)} nicht (noch nicht) wiederherstellbar")
    for s in bad[:30]:
        print(f"  Stripe {s.stripe + 1}: {s.present}/{s.needed} benötigte Pakete")
    return 0 if not bad and man["status"] == "complete" else 2


def cmd_config(a) -> int:
    """Read or change settings without the window (used by the install script)."""
    from vidtransfer import desktop
    from vidtransfer.drive import find_drive_locations

    cfg = Config.load()
    if a.list_drives:
        for p in find_drive_locations():
            print(p.as_posix())          # "/" also on Windows, so shell scripts can use it
        return 0
    if a.get:
        value = {
            "drive": cfg.drive_root or "",
            "sender": cfg.sender_name or desktop.computer_name(),
            "inbox": cfg.receive_dir or str(desktop.default_inbox()),
            "mode": cfg.mode,
            "login": "on" if desktop.launch_at_login_enabled() else "off",
        }[a.get]
        print(value)
        return 0

    changes: dict = {}
    if a.drive is not None:
        drive = Path(a.drive).expanduser()
        if not drive.is_dir():
            print(f"Fehler: Ordner nicht gefunden: {drive}", file=sys.stderr)
            return 1
        changes["drive_root"] = str(drive)
    if a.sender is not None:
        changes["sender_name"] = a.sender.strip() or None
    if a.inbox is not None:
        inbox = Path(a.inbox).expanduser()
        inbox.mkdir(parents=True, exist_ok=True)
        changes["receive_dir"] = str(inbox)
    if a.mode is not None:
        changes["mode"] = a.mode
    if changes:
        cfg.update(**changes)
    if a.login is not None:
        if desktop.IS_MAC:
            desktop.set_launch_at_login(a.login == "on")
        else:
            print("Hinweis: Start beim Anmelden gibt es nur unter macOS.", file=sys.stderr)
    if not changes and a.login is None:
        for key in ("drive_root", "sender_name", "receive_dir", "mode", "auto_receive"):
            print(f"{key} = {getattr(cfg, key)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windowed apps (PyInstaller, console=False) may have no stdout/stderr
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    # macOS may pass -psn_… when launched from Finder
    argv = [x for x in argv if not x.startswith("-psn_")]

    if not argv or argv[0] not in COMMANDS and not argv[0].startswith("-"):
        setup_logging()
        from vidtransfer.gui import run_gui
        return run_gui(argv)

    ap = argparse.ArgumentParser(prog="vidtransfer", description="Fehlertolerante Übertragung über Google Drive")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="Ordner paketieren und in den Drive-Ordner schreiben")
    s.add_argument("source")
    s.add_argument("--drive", help="Google-Drive-Ordner (Standard: automatisch)")
    s.add_argument("--k", type=int, help="Datenpakete pro Stripe (Standard 10)")
    s.add_argument("--m", type=int, help="Paritätspakete pro Stripe (Standard 3)")
    s.add_argument("--block-mib", type=int, help="Paketgröße in MiB (Standard 32)")
    s.add_argument("--zstd-level", type=int, help="0 = keine Kompression (Standard 3)")
    s.set_defaults(func=cmd_send)

    r = sub.add_parser("receive", help="Transfer-Ordner prüfen, reparieren und entpacken "
                                       "(setzt einen abgebrochenen Empfang automatisch fort)")
    r.add_argument("folder")
    r.add_argument("dest", help="Zielverzeichnis (darin wird der Ordner angelegt)")
    r.add_argument("--no-wait", action="store_true", help="nicht auf fehlende Pakete warten")
    r.add_argument("--only", action="append", metavar="MUSTER",
                   help="nur passende Dateien, z. B. \"Audio/*.wav\" oder \"clip01.mp4\" (mehrfach möglich)")
    r.add_argument("--salvage", action="store_true",
                   help="bei endgültig fehlenden Teilen alles andere wiederherstellen (Ordner \"… (incomplete)\")")
    r.add_argument("--without-manifest", action="store_true",
                   help="Notfall: ohne Manifest nur aus den Paketen wiederherstellen")
    r.set_defaults(func=cmd_receive)

    v = sub.add_parser("verify", help="alle Pakete und den Inhalt prüfen, ohne zu entpacken")
    v.add_argument("folder")
    v.add_argument("--quick", action="store_true", help="nur Pakete prüfen, Inhalt nicht dekodieren")
    v.add_argument("--without-manifest", action="store_true", help="Notfall: ohne Manifest prüfen")
    v.set_defaults(func=cmd_verify)

    ls = sub.add_parser("list", help="Dateien eines Transfers anzeigen (lädt nichts)")
    ls.add_argument("folder")
    ls.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="schneller Überblick über einen Transfer-Ordner (lädt nichts)")
    st.add_argument("folder")
    st.set_defaults(func=cmd_status)

    c = sub.add_parser("config", help="Einstellungen anzeigen oder setzen (z. B. für das Installationsskript)")
    c.add_argument("--list-drives", action="store_true", help="gefundene Google-Drive-Ordner auflisten")
    c.add_argument("--get", choices=["drive", "sender", "inbox", "mode", "login"], help="einen Wert ausgeben")
    c.add_argument("--drive", help="Google-Drive-Ordner für die Lieferungen")
    c.add_argument("--sender", help="Name dieses Computers (leer = Computername)")
    c.add_argument("--inbox", help="Ablage für empfangene Lieferungen")
    c.add_argument("--mode", choices=["send", "receive"], help="Modus beim Start")
    c.add_argument("--login", choices=["on", "off"], help="beim Anmelden starten (macOS)")
    c.set_defaults(func=cmd_config)

    g = sub.add_parser("gui", help="Fenster öffnen")
    g.add_argument("paths", nargs="*")
    g.set_defaults(func=None)

    a = ap.parse_args(argv)
    setup_logging(a.verbose)
    if a.cmd == "gui":
        from vidtransfer.gui import run_gui
        return run_gui(a.paths)
    try:
        return a.func(a)
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        return 130
    except Cancelled:
        return 130
    except Exception as exc:  # noqa: BLE001 – print readable message for the console
        logging.getLogger(__name__).exception("Command %s failed", a.cmd)
        print(f"\nFehler: {exc}", file=sys.stderr)
        return 1
