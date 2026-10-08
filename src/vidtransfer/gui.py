"""Tk-Oberfläche.

Bedienung auf dem Mac: Ordner auf das App-Symbol (Dock oder Finder) ziehen.
- normaler Ordner          → wird gesendet (Pakete in Google Drive)
- Transfer-Ordner im Drive → wird empfangen (Ziel wird abgefragt)
Mehrere Ordner werden nacheinander abgearbeitet.
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

from vidtransfer import APP_NAME, __version__
from vidtransfer.config import Config, log_dir
from vidtransfer.drive import default_cloudstorage_dir, find_drive_roots
from vidtransfer.manifest import is_transfer_folder
from vidtransfer.util import Cancelled, human_bytes

log = logging.getLogger(__name__)
IS_MAC = sys.platform == "darwin"


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
    """Verhindert den Ruhezustand, solange die App läuft (Drive soll weiter hochladen)."""
    if IS_MAC:
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            pass


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

    # ------------------------------------------------------------ UI
    def _build(self) -> None:
        r = self.root
        r.title(APP_NAME)
        r.geometry("640x500")
        r.minsize(540, 420)
        main = ttk.Frame(r, padding=16)
        main.pack(fill="both", expand=True)

        drop = ttk.LabelFrame(main, text="So geht's", padding=12)
        drop.pack(fill="x")
        ttk.Label(drop, justify="left", text=(
            "Ordner auf das VidTransfer-Symbol im Dock ziehen:\n"
            "  •  Video-/Audio-Ordner  →  wird nach Google Drive gesendet\n"
            "  •  Transfer-Ordner aus Google Drive  →  wird wiederhergestellt"
        )).pack(anchor="w")

        btns = ttk.Frame(main)
        btns.pack(fill="x", pady=(12, 4))
        ttk.Button(btns, text="Ordner senden …", command=self._pick_send).pack(side="left")
        ttk.Button(btns, text="Aus Drive empfangen …", command=self._pick_receive).pack(side="left", padx=8)

        drv = ttk.Frame(main)
        drv.pack(fill="x", pady=(4, 8))
        ttk.Label(drv, text="Google Drive:").pack(side="left")
        self.drive_var = tk.StringVar(value=self.cfg.drive_root or "(wird beim ersten Senden gesucht)")
        ttk.Label(drv, textvariable=self.drive_var, foreground="gray").pack(side="left", padx=6)
        ttk.Button(drv, text="Ändern …", command=lambda: self._drive_root(ask=True)).pack(side="right")

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
        ttk.Button(bottom, text="Log öffnen", command=self._open_logs).pack(side="left", padx=8)
        ttk.Label(bottom, text=f"v{__version__}", foreground="gray").pack(side="right")

    def _log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------ Eingaben
    def _on_open_document(self, *paths: str) -> None:
        for p in paths:
            self.add_path(p)

    def _pick_send(self) -> None:
        p = filedialog.askdirectory(title="Ordner zum Senden wählen", mustexist=True)
        if p:
            self.add_path(p, mode="send")

    def _pick_receive(self) -> None:
        initial = self.cfg.drive_root or str(default_cloudstorage_dir())
        p = filedialog.askdirectory(title="Transfer-Ordner in Google Drive wählen",
                                    initialdir=initial, mustexist=True)
        if p:
            self.add_path(p, mode="receive")

    def _drive_root(self, ask: bool = False) -> Path | None:
        cur = self.cfg.drive_root
        if not ask and cur and Path(cur).is_dir():
            return Path(cur)
        roots = find_drive_roots()
        if not ask and len(roots) == 1:
            chosen = str(roots[0])
        else:
            opts = dict(title="Google-Drive-Ordner wählen („Meine Ablage“ / „My Drive“)",
                        initialdir=str(roots[0] if roots else default_cloudstorage_dir()), mustexist=True)
            if IS_MAC:
                opts["message"] = opts["title"]
            chosen = filedialog.askdirectory(**opts)
            if not chosen:
                return None
        self.cfg.drive_root = chosen
        self.cfg.save()
        self.drive_var.set(chosen)
        self._log(f"Google-Drive-Ordner: {chosen}")
        return Path(chosen)

    def add_path(self, raw: str, mode: str | None = None) -> None:
        path = Path(raw)
        if not path.is_dir():
            messagebox.showwarning(APP_NAME, f"Bitte einen Ordner ziehen, keine Datei:\n{path.name}")
            return
        if mode is None:
            mode = "receive" if is_transfer_folder(path) else "send"
        elif mode == "receive" and not is_transfer_folder(path):
            messagebox.showerror(APP_NAME, f"„{path.name}“ ist kein VidTransfer-Ordner (manifest.json fehlt).")
            return

        if mode == "send":
            drive = self._drive_root()
            if drive is None:
                return
            job = ("send", path, drive)
        else:
            initial = self.cfg.receive_dir or str(Path.home() / "Downloads")
            dest = filedialog.askdirectory(title=f"Wohin soll „{path.name}“ wiederhergestellt werden?",
                                           initialdir=initial, mustexist=True)
            if not dest:
                return
            self.cfg.receive_dir = dest
            self.cfg.save()
            job = ("receive", path, Path(dest))

        self.pending += 1
        self._log(("Senden: " if mode == "send" else "Empfangen: ") + path.name
                  + ("  (in Warteschlange)" if self.busy else ""))
        self.jobs.put(job)

    # ------------------------------------------------------------ Hintergrund
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
                if kind == "send":
                    from vidtransfer.sender import send_folder

                    res = send_folder(path, target, self.cfg.send_options(), progress, self.cancel)
                    text = (f"„{path.name}“ ist paketiert: {human_bytes(res.source_bytes)} in "
                            f"{res.packets_written + res.packets_skipped} Paketen → {res.target}.\n"
                            "Google Drive lädt jetzt hoch – App bitte geöffnet lassen, "
                            "damit der Mac nicht einschläft.")
                else:
                    from vidtransfer.receiver import receive_folder

                    res = receive_folder(path, target, progress, self.cancel)
                    text = (f"„{res.dest.name}“ wiederhergestellt: {res.files} Dateien, "
                            f"{human_bytes(res.bytes)}, alle Prüfsummen ok"
                            + (f", {res.repaired_stripes} Stripe(s) repariert" if res.repaired_stripes else "")
                            + f".\n→ {res.dest}")
                self.events.put(("done", True, text))
            except Cancelled:
                self.events.put(("done", False, f"„{path.name}“: abgebrochen."))
            except Exception as exc:  # noqa: BLE001
                log.error("Fehler bei %s %s\n%s", kind, path, traceback.format_exc())
                self.events.put(("done", False, f"Fehler bei „{path.name}“: {exc}"))

    def _poll(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "start":
                    self.busy = True
                    self.cancel_btn.configure(state="normal")
                    self.progress.configure(value=0)
                    self.status_var.set(("Sende " if ev[1] == "send" else "Empfange ") + ev[2] + " …")
                elif ev[0] == "progress":
                    _, done, total, msg = ev
                    if total:
                        self.progress.configure(value=1000 * min(done, total) / total)
                    self.status_var.set(msg)
                elif ev[0] == "done":
                    _, ok, text = ev
                    self.pending -= 1
                    self.busy = self.pending > 0
                    if not self.busy:
                        self.cancel_btn.configure(state="disabled")
                    self.progress.configure(value=1000 if ok else 0)
                    self.status_var.set(text.splitlines()[0])
                    self._log(("✔ " if ok else "✖ ") + text)
                    _notify(APP_NAME, text.splitlines()[0])
                    if not ok and "abgebrochen" not in text:
                        messagebox.showerror(APP_NAME, text)
                    if self.closing and not self.busy:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # ------------------------------------------------------------ Steuerung
    def _cancel(self) -> None:
        # Warteschlange leeren und laufenden Auftrag abbrechen
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
                                                 "(Ein abgebrochenes Senden kann später fortgesetzt werden.)"):
                return
            self.closing = True
            self._cancel()
            return
        self.root.destroy()

    def _open_logs(self) -> None:
        d = str(log_dir())
        if IS_MAC:
            subprocess.Popen(["open", d])
        elif sys.platform == "win32":
            os.startfile(d)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", d])


def run_gui(paths: list[str] | None = None) -> int:
    root = tk.Tk()
    App(root, list(paths or []))
    root.mainloop()
    return 0
