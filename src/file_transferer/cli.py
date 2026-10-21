"""Command line interface. Without arguments (or with paths only) the GUI starts."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from file_transferer import __version__
from file_transferer.config import Config, setup_logging
from file_transferer.drive import find_drive_roots
from file_transferer.util import Cancelled, human_bytes

COMMANDS = {"send", "receive", "verify", "status", "list", "config", "diag", "gui"}


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
        raise SystemExit("Cloud folder (Google Drive) not found – please pass it with --drive.")
    raise SystemExit("Several cloud folders found, please choose one with --drive:\n  "
                     + "\n  ".join(map(str, roots)))


def cmd_send(a) -> int:
    from file_transferer.sender import MIB, send_folder

    opts = Config.load().send_options()
    for name in ("k", "m", "zstd_level"):
        if getattr(a, name) is not None:
            setattr(opts, name, getattr(a, name))
    if a.block_mib:
        opts.block_size = a.block_mib * MIB
    res = send_folder(Path(a.source), _resolve_drive(a.drive), opts, _console_progress())
    print(f"\nTarget: {res.target}\n{human_bytes(res.source_bytes)} → {human_bytes(res.stream_bytes)} "
          f"compressed, {res.stripes} stripes, {res.packets_written} packets written, "
          f"{res.packets_skipped} skipped")
    return 0


def cmd_receive(a) -> int:
    from file_transferer.messages import receive_summary
    from file_transferer.receiver import StripeUnavailableError, receive_folder

    try:
        res = receive_folder(Path(a.folder), Path(a.dest), _console_progress(), wait=not a.no_wait,
                             only=a.only, salvage=a.salvage, without_manifest=a.without_manifest)
    except StripeUnavailableError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        if exc.upload_complete:
            print("Parts of the transfer are lost for good. --salvage restores everything "
                  "else.", file=sys.stderr)
        return 2
    print("\n" + receive_summary(res))
    return 0 if res.complete else 2


def cmd_verify(a) -> int:
    from file_transferer.messages import verify_summary
    from file_transferer.receiver import verify_transfer

    res = verify_transfer(Path(a.folder), _console_progress(), deep=not a.quick,
                          without_manifest=a.without_manifest)
    print("\n" + verify_summary(res))
    return 0 if res.ok or (a.quick and res.complete and not res.unrecoverable) else 2


def cmd_list(a) -> int:
    from file_transferer.recovery import list_transfer

    man, files = list_transfer(Path(a.folder))
    if not man:
        print("No valid manifest found.")
        return 1
    state = {True: "ok", False: "LOST", None: "?"}
    for f in files:
        print(f"{state[f.available]:>5}  {human_bytes(f.size):>10}  {f.compression:>4}  {f.path}")
    missing = sum(f.available is False for f in files)
    print(f"{len(files)} files, {human_bytes(sum(f.size for f in files))}"
          + (f", {missing} currently not recoverable" if missing else ""))
    return 0 if not missing else 2


def cmd_status(a) -> int:
    from file_transferer import watch
    from file_transferer.messages import completeness_report
    from file_transferer.receiver import transfer_status

    check = watch.check_delivery(Path(a.folder))
    if a.missing:                    # plain list for scripts: one packet name per line, file names only
        if check is None:
            print("No valid manifest found.", file=sys.stderr)
            return 1
        print("\n".join(check.missing_names))
        return 0 if not check.missing_names else 2
    man, stripes = transfer_status(Path(a.folder))
    if not man:
        print("No valid manifest found.")
        return 1
    p = man["params"]
    print(f"{man['name']}  [{man['status']}]  k={p['k']} m={p['m']} "
          f"packet={human_bytes(p['block_size'])}  source={human_bytes(man['source_bytes'])}")
    bad = [s for s in stripes if not s.recoverable]
    full = sum(s.present == s.total for s in stripes)
    print(f"{len(stripes)} stripes: {full} complete, {len(stripes) - full - len(bad)} repairable, "
          f"{len(bad)} not (yet) recoverable")
    for s in bad[:30]:
        print(f"  Stripe {s.stripe + 1}: {s.present}/{s.needed} needed packets")
    if check is not None and check.missing_names:
        print("\n" + completeness_report(check) + "\n(all names: file-transferer status --missing)")
    return 0 if not bad and man["status"] == "complete" else 2


def cmd_diag(a) -> int:
    from file_transferer import diag

    text = diag.collect(Path(a.path).expanduser())
    out = Path(a.output).expanduser() if a.output else diag.default_output()
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"Saved to {out} - please send this file.")
    return 0


def cmd_config(a) -> int:
    """Read or change settings without the window (used by the install script)."""
    from file_transferer import desktop
    from file_transferer.drive import find_drive_locations

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
            print(f"Error: folder not found: {drive}", file=sys.stderr)
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
            print("Note: start at login is only available on macOS.", file=sys.stderr)
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
        from file_transferer.gui import run_gui
        return run_gui(argv)

    ap = argparse.ArgumentParser(prog="file-transferer", description="Fault-tolerant transfer via a cloud folder")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="pack a folder and write it to the cloud folder")
    s.add_argument("source")
    s.add_argument("--drive", help="cloud folder (default: Google Drive, found automatically)")
    s.add_argument("--k", type=int, help="data packets per stripe (default 10)")
    s.add_argument("--m", type=int, help="parity packets per stripe (default 3)")
    s.add_argument("--block-mib", type=int, help="packet size in MiB (default 32)")
    s.add_argument("--zstd-level", type=int, help="0 = no compression (default 3)")
    s.set_defaults(func=cmd_send)

    r = sub.add_parser("receive", help="verify, repair and unpack a transfer folder "
                                       "(resumes an interrupted receive automatically)")
    r.add_argument("folder")
    r.add_argument("dest", help="target directory (the folder is created inside it)")
    r.add_argument("--no-wait", action="store_true", help="do not wait for missing packets")
    r.add_argument("--only", action="append", metavar="PATTERN",
                   help="only matching files, e.g. \"Audio/*.wav\" or \"clip01.mp4\" (repeatable)")
    r.add_argument("--salvage", action="store_true",
                   help="if parts are lost for good, restore everything else (folder \"… (incomplete)\")")
    r.add_argument("--without-manifest", action="store_true",
                   help="emergency: restore from the packets alone, without a manifest")
    r.set_defaults(func=cmd_receive)

    v = sub.add_parser("verify", help="check all packets and the content without unpacking")
    v.add_argument("folder")
    v.add_argument("--quick", action="store_true", help="check packets only, do not decode the content")
    v.add_argument("--without-manifest", action="store_true", help="emergency: verify without a manifest")
    v.set_defaults(func=cmd_verify)

    ls = sub.add_parser("list", help="show the files of a transfer (downloads nothing)")
    ls.add_argument("folder")
    ls.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="quick overview of a transfer folder (downloads nothing)")
    st.add_argument("folder")
    st.add_argument("--missing", action="store_true",
                    help="only list the missing packet files, one per line (reads file names only)")
    st.set_defaults(func=cmd_status)

    dg = sub.add_parser("diag", help="collect what macOS reports about files in a cloud folder "
                                     "(read-only; to find out whether upload progress can be shown)")
    dg.add_argument("path", help="a file, or a delivery folder inside the cloud folder")
    dg.add_argument("-o", "--output", help="where to save the report (default: home folder)")
    dg.set_defaults(func=cmd_diag)

    c = sub.add_parser("config", help="show or change settings (e.g. for the install script)")
    c.add_argument("--list-drives", action="store_true", help="list the cloud folders found (Google Drive)")
    c.add_argument("--get", choices=["drive", "sender", "inbox", "mode", "login"], help="print one value")
    c.add_argument("--drive", help="cloud folder for deliveries")
    c.add_argument("--sender", help="name of this computer (empty = computer name)")
    c.add_argument("--inbox", help="folder for received deliveries")
    c.add_argument("--mode", choices=["send", "receive"], help="mode at start")
    c.add_argument("--login", choices=["on", "off"], help="start at login (macOS)")
    c.set_defaults(func=cmd_config)

    g = sub.add_parser("gui", help="open the window")
    g.add_argument("paths", nargs="*")
    g.set_defaults(func=None)

    a = ap.parse_args(argv)
    setup_logging(a.verbose)
    if a.cmd == "gui":
        from file_transferer.gui import run_gui
        return run_gui(a.paths)
    try:
        return a.func(a)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Cancelled:
        return 130
    except Exception as exc:  # noqa: BLE001 – print readable message for the console
        logging.getLogger(__name__).exception("Command %s failed", a.cmd)
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
