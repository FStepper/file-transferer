"""Tk user interface (UI language: German).

Usage on the Mac: drag folders onto the app icon (Dock or Finder).
- a regular folder              → is sent (packets go to Google Drive)
- a transfer folder from Drive  → is received (destination is asked for)
Several folders are processed one after another.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from vidtransfer import APP_NAME, __version__
from vidtransfer.config import Config, log_dir
from vidtransfer.drive import default_cloudstorage_dir, find_drive_roots
from vidtransfer.manifest import is_transfer_folder
from vidtransfer.util import Cancelled, human_bytes

log = logging.getLogger(__name__)
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# Outcome of a job, used for icon, dialogs and notifications
OK, WARN, ERROR, CANCELLED = "ok", "warn", "error", "cancelled"


def _notify(title: str, text: str) -> None:
    if not IS_MAC:
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    try:
        subprocess.Popen(["osascript", "-e",
                          f'display notification "{esc(text)}" with title "{esc(title)}" sound name "Glass"'])
    except OSError:
        pass


def _keep_awake() -> None:
    """Prevent idle sleep while the app is running (Drive must keep uploading)."""
    if IS_MAC:
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            pass


def _reveal(path: Path) -> None:
    """Show `path` in Finder / Explorer."""
    try:
        if IS_MAC:
            subprocess.Popen(["open", "-R", str(path)])
        elif IS_WIN:
            subprocess.Popen(["explorer", f"/select,{path}"])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError:
        log.exception("Could not reveal %s", path)


def _quick_status(path: Path) -> Optional[str]:
    """One-line overview of a transfer folder (headers only, downloads nothing)."""
    from vidtransfer.receiver import transfer_status

    try:
        man, stripes = transfer_status(path)
    except Exception:  # noqa: BLE001 – purely informational
        log.exception("Quick status failed for %s", path)
        return None
    if not man:
        return None
    full = sum(s.present == s.total for s in stripes)
    bad = sum(not s.recoverable for s in stripes)
    state = "Upload abgeschlossen" if man.get("status") == "complete" else "Upload läuft noch"
    return (f"Status: {state}, {len(stripes)} Stripes – {full} vollständig, "
            f"{len(stripes) - full - bad} reparierbar, {bad} noch unvollständig "
            f"({human_bytes(man.get('source_bytes', 0))})")


class App:
    def __init__(self, root: tk.Tk, paths: list[str]):
        self.root = root
        self.cfg = Config.load()
        self.jobs: "queue.Queue[tuple]" = queue.Queue()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.pending = 0
        self.closing = False
        self.last_result: Optional[Path] = None

        self._build()
        threading.Thread(target=self._worker, daemon=True).start()
        root.after(100, self._poll)

        if IS_MAC:
            root.createcommand("::tk::mac::OpenDocument", self._on_open_document)
            root.createcommand("::tk::mac::ReopenApplication", root.deiconify)
            root.createcommand("::tk::mac::Quit", self._on_close)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        if paths:
            root.after(300, lambda: [self.add_path(p) for p in paths])
        _keep_awake()

    # ------------------------------------------------------------ layout
    def _build(self) -> None:
        r = self.root
        r.title(APP_NAME)
        r.geometry("680x520")
        r.minsize(580, 440)
        main = ttk.Frame(r, padding=16)
        main.pack(fill="both", expand=True)

        drop = ttk.LabelFrame(main, text="So geht's", padding=12)
        drop.pack(fill="x")
        where = "das VidTransfer-Symbol im Dock" if IS_MAC else "VidTransfer.exe (oder eine Verknüpfung)"
        ttk.Label(drop, justify="left", text=(
            f"Ordner auf {where} ziehen – oder die Buttons nutzen:\n"
            "  •  Video-/Audio-Ordner  →  wird nach Google Drive gesendet\n"
            "  •  Transfer-Ordner aus Google Drive  →  wird wiederhergestellt\n"
            "Ein abgebrochener Vorgang wird beim nächsten Mal automatisch fortgesetzt."
        )).pack(anchor="w")

        btns = ttk.Frame(main)
        btns.pack(fill="x", pady=(12, 4))
        ttk.Button(btns, text="Ordner senden …", command=self._pick_send).pack(side="left")
        ttk.Button(btns, text="Aus Drive empfangen …", command=self._pick_receive).pack(side="left", padx=8)
        ttk.Button(btns, text="Nur prüfen …", command=self._pick_verify).pack(side="left")

        drv = ttk.Frame(main)
        drv.pack(fill="x", pady=(4, 8))
        ttk.Label(drv, text="Senden nach:").pack(side="left")
        self.drive_var = tk.StringVar()
        ttk.Label(drv, textvariable=self.drive_var, foreground="gray").pack(side="left", padx=6)
        ttk.Button(drv, text="Ändern …", command=lambda: self._drive_root(ask=True)).pack(side="right")
        self._show_drive_root()

        self.progress = ttk.Progressbar(main, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", pady=(8, 2))
        self.status_var = tk.StringVar(value="Bereit.")
        ttk.Label(main, textvariable=self.status_var).pack(anchor="w")

        logf = ttk.Frame(main)
        logf.pack(fill="both", expand=True, pady=(8, 8))
        self.log_text = tk.Text(logf, height=10, wrap="word", state="disabled", relief="flat",
                                font=("Menlo", 11) if IS_MAC else ("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bottom = ttk.Frame(main)
        bottom.pack(fill="x")
        self.cancel_btn = ttk.Button(bottom, text="Abbrechen", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        reveal_text = "Im Finder zeigen" if IS_MAC else "Im Explorer zeigen" if IS_WIN else "Ordner öffnen"
        self.reveal_btn = ttk.Button(bottom, text=reveal_text, state="disabled",
                                     command=lambda: self.last_result and _reveal(self.last_result))
        self.reveal_btn.pack(side="left", padx=8)
        ttk.Button(bottom, text="Log öffnen", command=self._open_logs).pack(side="left")
        ttk.Label(bottom, text=f"v{__version__}", foreground="gray").pack(side="right")

    def _log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------ input
    def _reload_config(self) -> None:
        """Pick up settings another app window may have changed."""
        self.cfg = Config.load()
        self._show_drive_root()

    def _show_drive_root(self) -> None:
        cur = self.cfg.drive_root
        if not cur:
            self.drive_var.set("(Google Drive wird beim ersten Senden gesucht)")
            return
        local = Path(cur) not in find_drive_roots()
        self.drive_var.set(cur + ("   (eigener Ordner, z. B. zum lokalen Testen)" if local else ""))

    def _ask_dir(self, title: str, *initial: Optional[str]) -> Optional[Path]:
        """Folder dialog starting in the first existing of `initial`."""
        opts = dict(title=title, mustexist=True)
        start = next((d for d in initial if d and Path(d).is_dir()), None)
        if start:
            opts["initialdir"] = str(start)
        if IS_MAC:
            opts["message"] = title
        chosen = filedialog.askdirectory(**opts)
        return Path(chosen) if chosen else None   # Path() also normalises "/" on Windows

    def _on_open_document(self, *paths: str) -> None:
        for p in paths:
            self.add_path(p)

    def _pick_send(self) -> None:
        self._reload_config()
        p = self._ask_dir("Ordner zum Senden wählen", self.cfg.last_send_parent, str(Path.home()))
        if p:
            self.add_path(str(p), mode="send")

    def _pick_transfer(self, title: str) -> Optional[Path]:
        self._reload_config()
        return self._ask_dir(title, self.cfg.last_transfer_parent, self.cfg.drive_root,
                             str(default_cloudstorage_dir()))

    def _pick_receive(self) -> None:
        p = self._pick_transfer("Transfer-Ordner wählen (in Google Drive oder im Testordner)")
        if p:
            self.add_path(str(p), mode="receive")

    def _pick_verify(self) -> None:
        p = self._pick_transfer("Transfer-Ordner zum Prüfen wählen")
        if p:
            self.add_path(str(p), mode="verify")

    def _drive_root(self, ask: bool = False) -> Optional[Path]:
        """Target folder for sending: the saved one; otherwise the only Drive found; otherwise ask.

        Any folder can be chosen – e.g. a local folder to test without Google Drive.
        A choice made by the user is never replaced automatically.
        """
        self._reload_config()
        cur = self.cfg.drive_root
        if not ask and cur and Path(cur).is_dir():
            return Path(cur)
        if not ask and cur:
            messagebox.showwarning(APP_NAME, f"Der Zielordner zum Senden existiert nicht mehr:\n{cur}\n"
                                             "Bitte einen neuen wählen.")
        roots = find_drive_roots()
        if not ask and not cur and len(roots) == 1:
            chosen = roots[0]
        else:
            chosen = self._ask_dir("Zielordner zum Senden wählen (Google Drive „Meine Ablage“ oder "
                                   "ein lokaler Testordner)",
                                   cur, *(str(r) for r in roots), str(default_cloudstorage_dir()))
            if chosen is None:
                return None
        self.cfg.update(drive_root=str(chosen))
        self._show_drive_root()
        self._log(f"Senden nach: {chosen}")
        return chosen

    def add_path(self, raw: str, mode: Optional[str] = None) -> None:
        self._reload_config()
        path = Path(raw)
        if not path.is_dir():
            messagebox.showwarning(APP_NAME, f"Bitte einen Ordner ziehen, keine Datei:\n{path.name}")
            return
        if mode is None:
            mode = "receive" if is_transfer_folder(path) else "send"
        elif mode in ("receive", "verify") and not is_transfer_folder(path):
            messagebox.showerror(APP_NAME, f"„{path.name}“ ist kein VidTransfer-Ordner (manifest.json fehlt).")
            return

        if mode == "send":
            drive = self._drive_root()
            if drive is None:
                return
            self.cfg.update(last_send_parent=str(path.parent))
            job = ("send", path, drive)
        else:
            self.cfg.update(last_transfer_parent=str(path.parent))
            if mode == "verify":
                job = ("verify", path, None)
            else:
                dest = self._ask_dir(f"Wohin soll „{path.name}“ wiederhergestellt werden?",
                                     self.cfg.receive_dir, str(Path.home() / "Downloads"), str(Path.home()))
                if dest is None:
                    return
                self.cfg.update(receive_dir=str(dest))
                job = ("receive", path, dest)

        self.pending += 1
        label = {"send": "Senden", "receive": "Empfangen", "verify": "Prüfen"}[mode]
        self._log(f"{label}: {path.name}" + ("  (in Warteschlange)" if self.busy else ""))
        self.jobs.put(job)

    # ------------------------------------------------------------ background work
    def _run_job(self, kind: str, path: Path, target, progress) -> tuple[str, str, Optional[Path]]:
        """Run one job in the worker thread. Returns (outcome, text, path to reveal)."""
        if kind == "send":
            from vidtransfer.sender import send_folder

            res = send_folder(path, target, self.cfg.send_options(), progress, self.cancel)
            return OK, (f"„{path.name}“ ist paketiert: {human_bytes(res.source_bytes)} in "
                        f"{res.packets_written + res.packets_skipped} Paketen → {res.target}.\n"
                        "Google Drive lädt jetzt hoch – App bitte geöffnet lassen, "
                        "damit der Mac nicht einschläft."), res.target

        if kind == "verify":
            from vidtransfer.messages import verify_summary
            from vidtransfer.receiver import verify_transfer

            res = verify_transfer(path, progress, self.cancel)
            return (OK if res.ok else WARN), verify_summary(res), None

        from vidtransfer.messages import receive_summary
        from vidtransfer.receiver import receive_folder

        status = _quick_status(path)
        if status:
            self.events.put(("log", status))
        res = receive_folder(path, target, progress, self.cancel)
        return OK, receive_summary(res), res.dest

    def _worker(self) -> None:
        while True:
            kind, path, target = self.jobs.get()
            self.cancel.clear()
            self.events.put(("start", kind, path.name))
            last = [0.0]

            def progress(done, total, msg, _last=last):
                now = time.monotonic()
                if now - _last[0] >= 0.2 or done == total:
                    _last[0] = now
                    self.events.put(("progress", done, total, msg))

            try:
                outcome, text, reveal = self._run_job(kind, path, target, progress)
            except Cancelled:
                outcome, reveal = CANCELLED, None
                text = f"„{path.name}“: abgebrochen."
                if kind in ("send", "receive"):
                    text += " Beim nächsten Mal wird automatisch fortgesetzt" + (
                        " (gleiches Ziel wählen)." if kind == "receive" else ".")
            except Exception as exc:  # noqa: BLE001
                log.error("%s of %s failed\n%s", kind, path, traceback.format_exc())
                outcome, text, reveal = ERROR, f"Fehler bei „{path.name}“: {exc}", None
            self.events.put(("done", outcome, text, reveal))

    def _poll(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "start":
                    self.busy = True
                    self.cancel_btn.configure(state="normal")
                    self.progress.configure(value=0)
                    verb = {"send": "Sende", "receive": "Empfange", "verify": "Prüfe"}[ev[1]]
                    self.status_var.set(f"{verb} {ev[2]} …")
                elif ev[0] == "log":
                    self._log(ev[1])
                elif ev[0] == "progress":
                    _, done, total, msg = ev
                    if total:
                        self.progress.configure(value=1000 * min(done, total) / total)
                    self.status_var.set(msg)
                elif ev[0] == "done":
                    self._on_done(*ev[1:])
                    if self.closing and not self.busy:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _on_done(self, outcome: str, text: str, reveal: Optional[Path]) -> None:
        self.pending -= 1
        self.busy = self.pending > 0
        if not self.busy:
            self.cancel_btn.configure(state="disabled")
        self.progress.configure(value=1000 if outcome == OK else 0)
        first = text.splitlines()[0]
        self.status_var.set(first)
        icon = {OK: "✔", WARN: "⚠", ERROR: "✖", CANCELLED: "–"}[outcome]
        self._log(f"{icon} {text}")
        if reveal:
            self.last_result = reveal
            self.reveal_btn.configure(state="normal")
        if outcome != CANCELLED:
            _notify(APP_NAME, first)
        if outcome == ERROR:
            messagebox.showerror(APP_NAME, text)
        elif outcome == WARN:
            messagebox.showwarning(APP_NAME, text)

    # ------------------------------------------------------------ control
    def _cancel(self) -> None:
        # Drop queued jobs and cancel the running one
        while True:
            try:
                self.jobs.get_nowait()
                self.pending -= 1
            except queue.Empty:
                break
        self.cancel.set()
        self.status_var.set("Wird abgebrochen …")

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(APP_NAME, "Es läuft noch ein Vorgang. Abbrechen und beenden?\n"
                                                 "(Senden und Empfangen werden später automatisch fortgesetzt.)"):
                return
            self.closing = True
            self._cancel()
            return
        self.root.destroy()

    def _open_logs(self) -> None:
        d = str(log_dir())
        if IS_MAC:
            subprocess.Popen(["open", d])
        elif IS_WIN:
            os.startfile(d)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", d])


def run_gui(paths: Optional[list[str]] = None) -> int:
    root = tk.Tk()
    App(root, list(paths or []))
    root.mainloop()
    return 0
