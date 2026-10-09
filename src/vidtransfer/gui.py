"""Main window (Tk; UI language: German).

One window, two modes – "Senden" and "Empfangen" – switched at the top; the
last mode is remembered. Each mode shows one drop zone and one list of
deliveries ("cards"). Each card has one status line and at most one button.
Everything technical (log, manual verify, parameters) lives in the settings
window or behind "Details".

Threading: one worker thread runs the jobs one after another and only posts
events; all widgets and the delivery list are changed on the Tk main thread.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from vidtransfer import APP_NAME, __version__, desktop
from vidtransfer.config import Config, log_dir
from vidtransfer.drive import default_cloudstorage_dir, find_drive_roots
from vidtransfer.friendly import ACTIONS, Eta, explain, running_text, size_line, when
from vidtransfer.history import (
    ACTIVE, DONE, FAILED, INCOMPLETE, PAUSED, QUEUED, RUNNING, Delivery, History, now_iso,
)
from vidtransfer.manifest import is_transfer_folder, read_manifest
from vidtransfer.util import Cancelled

log = logging.getLogger(__name__)
IS_MAC, IS_WIN = desktop.IS_MAC, desktop.IS_WIN
SEND, RECEIVE, VERIFY = "send", "receive", "verify"

SENT_HINT = "Google Drive lädt jetzt im Hintergrund hoch – bitte online bleiben, bis Google Drive fertig ist."


def _existing_dir(raw: Optional[str]) -> Optional[Path]:
    """`raw` if it is a directory, else its nearest existing parent (None if unset)."""
    if not raw:
        return None
    p = Path(raw)
    while not p.is_dir():
        if p.parent == p:
            return None
        p = p.parent
    return p


@dataclass
class Job:
    kind: str                              # send | receive | verify
    delivery_id: Optional[str]             # None for verify
    sources: list[str]
    target: Optional[str]
    name: str = ""
    options: dict = field(default_factory=dict)
    sender: Optional[str] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    messages: list[str] = field(default_factory=list)   # last technical messages (for "Details")


# ---------------------------------------------------------------- widgets

class DropZone(tk.Canvas):
    """Large dashed area: drop files/folders onto it, or click it / its button."""

    def __init__(self, parent, text: str, button_text: str, on_paths: Callable[[list[str]], None],
                 on_click: Callable[[], None], drop_enabled: bool, bg: str):
        super().__init__(parent, height=150, highlightthickness=0, bd=0, bg=bg)
        # Without tkdnd only the Dock icon accepts drops, so do not promise it here
        self.text = text if drop_enabled else text.replace("hierher ziehen", "auswählen")
        self.on_paths = on_paths
        self.on_click = on_click
        self._hover = False
        self.button = ttk.Button(self, text=button_text, command=on_click)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: on_click())
        if drop_enabled:
            from tkinterdnd2 import DND_FILES

            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<DropEnter>>", lambda e: self._set_hover(True) or e.action)
            self.dnd_bind("<<DropLeave>>", lambda e: self._set_hover(False) or e.action)
            self.dnd_bind("<<Drop>>", self._on_drop)

    def _set_hover(self, value: bool) -> None:
        self._hover = value
        self._draw()

    def _on_drop(self, event):
        self._set_hover(False)
        self.on_paths(list(self.tk.splitlist(event.data)))
        return event.action

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        color = "#3b82f6" if self._hover else "gray55"
        self.create_rectangle(4, 4, w - 4, h - 4, dash=(6, 4), outline=color, width=2)
        self.create_text(w / 2, h / 2 - 22, text=self.text, font=App.fonts["zone"], fill=color)
        self.create_window(w / 2, h / 2 + 22, window=self.button)


class Card(ttk.Frame):
    """One delivery: name, size, progress bar (while running), status, one button."""

    def __init__(self, parent, app: "App", d: Delivery):
        super().__init__(parent, padding=(4, 10, 4, 10))
        self.app = app
        self.d = d
        self.eta = Eta()
        self.columnconfigure(0, weight=1)
        self.title = ttk.Label(self, style="CardTitle.TLabel")
        self.meta = ttk.Label(self, style="Muted.TLabel")
        self.bar = ttk.Progressbar(self, mode="determinate", maximum=1000)
        self.status = ttk.Label(self, wraplength=520, justify="left")
        self.hint = ttk.Label(self, style="Muted.TLabel", wraplength=520, justify="left")
        self.button = ttk.Button(self, command=self._on_button)
        self.details = ttk.Label(self, text="Details", style="Link.TLabel", cursor="hand2")
        self.details.bind("<Button-1>", lambda e: app.show_details(self.d))
        self.title.grid(row=0, column=0, sticky="w")
        self.meta.grid(row=1, column=0, sticky="w")
        self.status.grid(row=3, column=0, sticky="w", pady=(4, 0))
        for w in (self, self.title, self.meta, self.status, self.hint):
            w.bind("<Button-2>" if IS_MAC else "<Button-3>", self._menu)
        self.render()

    def render(self) -> None:
        d = self.d
        self.title.configure(text=d.name)
        self.meta.configure(text=size_line(d.size, d.files, d.sender if d.kind == RECEIVE else None))
        if d.state == DONE:
            text = ("✔ Liegt in Google Drive" if d.kind == SEND else "✔ Angekommen") + \
                   (f" · {when(d.finished)}" if d.finished else "")
        elif d.state == PAUSED:
            text = "⏸ " + d.message
        elif d.state in (FAILED, INCOMPLETE):
            text = "⚠ " + d.message
        else:
            text = d.message
        self.status.configure(text=text)
        if d.state == RUNNING:
            self.bar.grid(row=2, column=0, columnspan=2, sticky="we", pady=(6, 0))
        else:
            self.bar.grid_remove()
        if d.hint:
            self.hint.configure(text=d.hint)
            self.hint.grid(row=4, column=0, sticky="w")
        else:
            self.hint.grid_remove()
        action = "stop" if d.state in ACTIVE else d.action
        if action:
            self.button.configure(text=ACTIONS[action])
            self.button.grid(row=3, column=1, sticky="e", padx=(8, 0))
        else:
            self.button.grid_remove()
        if d.state in (FAILED, INCOMPLETE) and d.details:
            self.details.grid(row=5, column=0, sticky="w")
        else:
            self.details.grid_remove()

    def progress(self, done: int, total: int, msg: str) -> None:
        if total:
            self.bar.configure(value=1000 * min(done, total) / total)
            if not self.d.size:
                self.d.size = total
                self.meta.configure(text=size_line(total, self.d.files,
                                                   self.d.sender if self.d.kind == RECEIVE else None))
        self.status.configure(text=running_text(self.d.kind, done, total, msg, self.eta))

    def _on_button(self) -> None:
        action = "stop" if self.d.state in ACTIVE else self.d.action
        if action:
            self.app.run_action(self.d, action)

    def _menu(self, event) -> None:
        menu = tk.Menu(self, tearoff=False)
        if self.d.result and Path(self.d.result).exists():
            menu.add_command(label=ACTIONS["reveal"], command=lambda: desktop.reveal(Path(self.d.result)))
        menu.add_command(label="Details", command=lambda: self.app.show_details(self.d))
        if self.d.state not in ACTIVE:
            menu.add_command(label="Aus der Liste entfernen", command=lambda: self.app.remove(self.d))
        menu.tk_popup(event.x_root, event.y_root)


class DeliveryList(ttk.Frame):
    """Vertical, scrollable list of cards (newest first)."""

    def __init__(self, parent, app: "App", kind: str, empty_text: str, bg: str):
        super().__init__(parent)
        self.app = app
        self.kind = kind
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=bg)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.columnconfigure(0, weight=1)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self._update_scroll())
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")
        self.empty = ttk.Label(self.inner, text=empty_text, style="Muted.TLabel")
        self.cards: dict[str, Card] = {}

    def _on_resize(self, event) -> None:
        self.canvas.itemconfigure(self._win, width=event.width)
        self._update_scroll()

    def _update_scroll(self) -> None:
        """Show the scrollbar only when the cards do not fit."""
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        needed = self.inner.winfo_reqheight() > self.canvas.winfo_height() > 1
        if needed and not self.scroll.winfo_ismapped():
            self.scroll.pack(side="right", fill="y", before=self.canvas)
        elif not needed and self.scroll.winfo_ismapped():
            self.scroll.pack_forget()
            self.canvas.yview_moveto(0)

    def _wheel(self, event) -> None:
        if self.scroll.winfo_ismapped() and str(event.widget).startswith(str(self)):
            self.canvas.yview_scroll(int(-event.delta / (1 if IS_MAC else 120)), "units")

    def rebuild(self, deliveries: list[Delivery]) -> None:
        for child in self.inner.winfo_children():
            if child is not self.empty:
                child.destroy()
        self.cards.clear()
        if not deliveries:
            self.empty.grid(row=0, column=0, sticky="w", pady=12)
            return
        self.empty.grid_remove()
        for row, d in enumerate(deliveries):
            card = Card(self.inner, self.app, d)
            card.grid(row=2 * row, column=0, sticky="we")
            ttk.Separator(self.inner).grid(row=2 * row + 1, column=0, sticky="we")
            self.cards[d.id] = card


# ---------------------------------------------------------------- app

class App:
    fonts: dict = {}

    def __init__(self, root: tk.Tk, paths: list[str]):
        self.root = root
        self.cfg = Config.load()
        self.history = History.load(self.cfg.history_path())
        self.queue: "queue.Queue[Job]" = queue.Queue()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.jobs: dict[str, Job] = {}          # delivery id -> job (queued or running)
        self.running: Optional[Job] = None
        self.closing = False
        self.awake = desktop.KeepAwake()
        self.drop_enabled = desktop.enable_drop(root)

        self._styles()
        self._build()
        threading.Thread(target=self._worker, name="vt-worker", daemon=True).start()
        root.after(100, self._poll)
        if IS_MAC:
            root.createcommand("::tk::mac::OpenDocument", lambda *p: self.handle_paths(list(p)))
            root.createcommand("::tk::mac::ReopenApplication", root.deiconify)
            root.createcommand("::tk::mac::Quit", self._on_close)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        if paths:
            root.after(300, lambda: self.handle_paths(paths))

    # ------------------------------------------------------------ layout
    def _styles(self) -> None:
        base = tkfont.nametofont("TkDefaultFont")
        size = base.cget("size")
        size = size if size > 0 else 13
        App.fonts = {
            "title": (base.cget("family"), size + 5, "bold"),
            "card": (base.cget("family"), size + 1, "bold"),
            "zone": (base.cget("family"), size + 2),
        }
        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=App.fonts["title"])
        style.configure("CardTitle.TLabel", font=App.fonts["card"])
        style.configure("Muted.TLabel", foreground="gray50")
        style.configure("Link.TLabel", foreground="#3b82f6")
        # Mode switch: a framed pair of toolbuttons, so both halves look clickable
        style.configure("Segment.TFrame", relief="solid", borderwidth=1)
        style.configure("Toolbutton", padding=(14, 4))
        self.bg = style.lookup("TFrame", "background") or self.root.cget("background")

    def _build(self) -> None:
        r = self.root
        r.title(APP_NAME)
        r.geometry("640x620")
        r.minsize(520, 460)
        main = ttk.Frame(r, padding=(18, 14, 18, 12))
        main.pack(fill="both", expand=True)

        head = ttk.Frame(main)
        head.pack(fill="x")
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Button(head, text="Einstellungen", command=self.open_settings).pack(side="right")
        switch = ttk.Frame(head, style="Segment.TFrame", padding=2)
        switch.pack(side="right", padx=16)
        self.mode_var = tk.StringVar(value=self.cfg.mode if self.cfg.mode in (SEND, RECEIVE) else SEND)
        for value, text in ((SEND, "Senden"), (RECEIVE, "Empfangen")):
            ttk.Radiobutton(switch, text=text, value=value, variable=self.mode_var, style="Toolbutton",
                            command=self._mode_changed).pack(side="left")

        self.pages = {}
        send = ttk.Frame(main)
        DropZone(send, "Ordner mit Videos hierher ziehen", "Ordner auswählen …",
                 self.handle_paths, self._pick_send, self.drop_enabled, self.bg).pack(fill="x", pady=(16, 10))
        self.send_list = DeliveryList(send, self, SEND, "Noch nichts gesendet.", self.bg)
        self.send_list.pack(fill="both", expand=True)
        self.pages[SEND] = send

        recv = ttk.Frame(main)
        self.inbox_var = tk.StringVar()
        ttk.Label(recv, textvariable=self.inbox_var, style="Muted.TLabel").pack(anchor="w", pady=(16, 0))
        DropZone(recv, "Lieferung aus Google Drive hierher ziehen", "Lieferung auswählen …",
                 self.handle_paths, self._pick_delivery, self.drop_enabled, self.bg).pack(fill="x", pady=(8, 10))
        self.recv_list = DeliveryList(recv, self, RECEIVE, "Noch nichts empfangen.", self.bg)
        self.recv_list.pack(fill="both", expand=True)
        self.pages[RECEIVE] = recv

        self._show_inbox()
        self._show_page()
        self.refresh()

    def _show_page(self) -> None:
        for mode, page in self.pages.items():
            if mode == self.mode_var.get():
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()

    def _mode_changed(self) -> None:
        self._show_page()
        self.cfg.update(mode=self.mode_var.get())

    def set_mode(self, mode: str) -> None:
        if self.mode_var.get() != mode:
            self.mode_var.set(mode)
            self._mode_changed()

    def refresh(self) -> None:
        self.send_list.rebuild(self.history.of_kind(SEND))
        self.recv_list.rebuild(self.history.of_kind(RECEIVE))

    def _card(self, d: Delivery) -> Optional[Card]:
        lst = self.send_list if d.kind == SEND else self.recv_list
        return lst.cards.get(d.id)

    def _render(self, d: Delivery) -> None:
        card = self._card(d)
        if card:
            card.render()

    # ------------------------------------------------------------ settings helpers
    def _reload_config(self) -> None:
        """Pick up settings another app window may have changed."""
        self.cfg = Config.load()
        self._show_inbox()

    def inbox(self) -> Path:
        return Path(self.cfg.receive_dir) if self.cfg.receive_dir else desktop.default_inbox()

    def _show_inbox(self) -> None:
        self.inbox_var.set(f"Empfangene Lieferungen landen in: {self.inbox()}")

    def sender_name(self) -> str:
        return self.cfg.sender_name or desktop.computer_name()

    def _ask_dir(self, title: str, *initial: Optional[str]) -> Optional[Path]:
        """Folder dialog starting in the first existing of `initial` (or its nearest parent)."""
        opts = dict(title=title, mustexist=True)
        start = next((d for d in map(_existing_dir, initial) if d), None)
        if start:
            opts["initialdir"] = str(start)
        if IS_MAC:
            opts["message"] = title
        chosen = filedialog.askdirectory(**opts)
        return Path(chosen) if chosen else None   # Path() also normalises "/" on Windows

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
            messagebox.showwarning(APP_NAME, f"Der Google-Drive-Ordner ist nicht erreichbar:\n{cur}\n\n"
                                             "Läuft Google Drive? Sonst bitte den Ordner neu wählen.")
        roots = find_drive_roots()
        if not ask and not cur and len(roots) == 1:
            chosen = roots[0]
        else:
            chosen = self._ask_dir("Google-Drive-Ordner wählen, in den gesendet wird",
                                   cur, *(str(r) for r in roots), str(default_cloudstorage_dir()))
            if chosen is None:
                return None
        self.cfg.update(drive_root=str(chosen))
        return chosen

    # ------------------------------------------------------------ input
    def _pick_send(self) -> None:
        self._reload_config()
        p = self._ask_dir("Ordner zum Senden wählen", self.cfg.last_send_dir, str(Path.home()))
        if p:
            self.handle_paths([str(p)])

    def _pick_delivery(self) -> None:
        self._reload_config()
        p = self._ask_dir("Lieferung in Google Drive wählen", self.cfg.last_transfer_dir,
                          self.cfg.drive_root, str(default_cloudstorage_dir()))
        if p:
            self.handle_paths([str(p)])

    def handle_paths(self, raw_paths: list[str]) -> None:
        """Dropped or chosen items: deliveries are received, folders/files are sent."""
        self._reload_config()
        paths = [Path(p) for p in raw_paths]
        missing = [p for p in paths if not p.exists()]
        deliveries = [p for p in paths if p.is_dir() and is_transfer_folder(p)]
        folders = [p for p in paths if p.is_dir() and p not in deliveries]
        files = [p for p in paths if p.is_file()]
        if missing:
            messagebox.showwarning(APP_NAME, "Nicht gefunden:\n" + "\n".join(p.name for p in missing))
        for p in deliveries:
            self.cfg.update(last_transfer_dir=str(p))
            self.start_receive(p)
        if folders or files:
            drive = self._drive_root()
            if drive is None:
                return
            for f in folders:
                self.cfg.update(last_send_dir=str(f))
                self.start_send(drive, [f], f.name, {})
            if files:
                name = f"Dateien {datetime.now():%Y-%m-%d %H.%M}"
                self.start_send(drive, files, name, {"files": True})
            self.set_mode(SEND)
        elif deliveries:
            self.set_mode(RECEIVE)

    def _busy_with(self, kind: str, sources: list[str]) -> bool:
        return any(d.kind == kind and d.sources == sources and d.state in ACTIVE for d in self.history.items)

    def start_send(self, drive: Path, sources: list[Path], name: str, options: dict) -> None:
        srcs = [str(s) for s in sources]
        if self._busy_with(SEND, srcs):
            return
        d = self.history.add(Delivery(SEND, name, srcs, str(drive), options=options,
                                      files=len(srcs) if options.get("files") else 0))
        self.refresh()
        self.submit(self._job_for(d))

    def start_receive(self, folder: Path, target: Optional[Path] = None) -> None:
        srcs = [str(folder)]
        if self._busy_with(RECEIVE, srcs):
            return
        options: dict = {}
        man = read_manifest(folder)
        if man is None:
            if not messagebox.askyesno(APP_NAME, (
                    f"Von „{folder.name}“ fehlt noch die Inhaltsliste.\n\n"
                    "Meist ist die Lieferung einfach noch nicht ganz in Google Drive angekommen – "
                    "dann bitte etwas warten.\n\nSoll trotzdem versucht werden, die Dateien "
                    "wiederherzustellen (Notfall)?")):
                return
            options["without_manifest"] = True
        inbox = target or self.inbox()
        inbox.mkdir(parents=True, exist_ok=True)
        d = Delivery(RECEIVE, (man or {}).get("name", folder.name), srcs, str(inbox), options=options,
                     sender=(man or {}).get("sender"), size=(man or {}).get("source_bytes", 0),
                     files=len((man or {}).get("files", [])))
        self.history.add(d)
        self.refresh()
        self.submit(self._job_for(d))

    def _job_for(self, d: Delivery) -> Job:
        return Job(d.kind, d.id, list(d.sources), d.target, d.name, dict(d.options), self.sender_name())

    def submit(self, job: Job) -> None:
        if job.delivery_id:
            self.jobs[job.delivery_id] = job
        self.queue.put(job)
        self.awake.start()

    # ------------------------------------------------------------ card actions
    def run_action(self, d: Delivery, action: str) -> None:
        if action == "stop":
            job = self.jobs.get(d.id)
            if job:
                job.cancel.set()
                d.message = "Wird angehalten …"
                self._render(d)
            return
        if action == "reveal":
            if d.result:
                desktop.reveal(Path(d.result))
            return
        if action == "open_drive":
            desktop.open_google_drive()
            return
        if action == "open_privacy":
            desktop.open_privacy_settings()
            return
        if action == "choose_inbox":
            new = self._ask_dir("Wo sollen empfangene Lieferungen gespeichert werden?", str(self.inbox()))
            if not new:
                return
            self.cfg.update(receive_dir=str(new))
            self._show_inbox()
            d.target = str(new)
        if action == "salvage":
            d.options["salvage"] = True
        elif action == "fetch_missing":
            d.options.pop("salvage", None)
        # resume / retry / resend / fetch_missing / salvage / choose_inbox: run the job again
        d.state, d.message, d.action, d.details, d.hint = QUEUED, "Wartet …", None, "", ""
        self.history.save()
        self._render(d)
        self.submit(self._job_for(d))

    def remove(self, d: Delivery) -> None:
        if d.state in ACTIVE:
            return
        self.history.items.remove(d)
        self.history.save()
        self.refresh()

    def show_details(self, d: Delivery) -> None:
        win = tk.Toplevel(self.root)
        win.title(f"Details – {d.name}")
        win.geometry("620x360")
        text = tk.Text(win, wrap="word", font=("Menlo", 11) if IS_MAC else ("Consolas", 9))
        info = "\n".join(filter(None, [
            f"Lieferung: {d.name}", f"Quelle: {', '.join(d.sources)}", f"Ziel: {d.target or '-'}",
            f"Ergebnis: {d.result}" if d.result else "", f"Status: {d.state}", "", d.details]))
        text.insert("1.0", info)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, padx=8, pady=8)
        bar = ttk.Frame(win, padding=8)
        bar.pack(fill="x")

        def copy():
            win.clipboard_clear()
            win.clipboard_append(info)

        ttk.Button(bar, text="Details kopieren", command=copy).pack(side="left")
        ttk.Button(bar, text="Schließen", command=win.destroy).pack(side="right")

    # ------------------------------------------------------------ background work
    def _worker(self) -> None:
        while True:
            job = self.queue.get()
            if job.cancel.is_set():
                self.events.put(("finished", job, None, Cancelled()))
                continue
            self.events.put(("started", job))

            def progress(done, total, msg, _job=job, _last=[0.0]):  # noqa: B006 – one list per job
                if msg and (not _job.messages or _job.messages[-1] != msg):
                    _job.messages.append(msg)
                    del _job.messages[:-30]
                now = time.monotonic()
                if now - _last[0] >= 0.2 or done == total:
                    _last[0] = now
                    self.events.put(("progress", _job, done, total, msg))

            try:
                result = self._run(job, progress)
                self.events.put(("finished", job, result, None))
            except BaseException as exc:  # noqa: BLE001 – reported on the card
                if not isinstance(exc, Cancelled):
                    log.error("%s job %s failed", job.kind, job.name, exc_info=True)
                self.events.put(("finished", job, None, exc))

    def _run(self, job: Job, progress):
        if job.kind == SEND:
            from vidtransfer.sender import send_files, send_folder

            opts = self.cfg.send_options()
            if job.options.get("files"):
                return send_files([Path(s) for s in job.sources], job.name, Path(job.target), opts,
                                  progress, job.cancel, sender=job.sender)
            return send_folder(Path(job.sources[0]), Path(job.target), opts, progress, job.cancel,
                               sender=job.sender)
        if job.kind == RECEIVE:
            from vidtransfer.receiver import receive_folder

            return receive_folder(Path(job.sources[0]), Path(job.target), progress, job.cancel,
                                  **job.options)
        from vidtransfer.receiver import verify_transfer

        return verify_transfer(Path(job.sources[0]), progress, job.cancel,
                               without_manifest=job.options.get("without_manifest", False))

    def _poll(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                getattr(self, "_on_" + ev[0])(*ev[1:])
        except queue.Empty:
            pass
        if self.closing and self.running is None and self.queue.empty():
            self.awake.stop()
            self.root.destroy()
            return
        self.root.after(100, self._poll)

    def _on_started(self, job: Job) -> None:
        self.running = job
        if job.kind == VERIFY:
            self.root.title(f"{APP_NAME} – „{job.name}“ wird geprüft …")
        d = self.history.get(job.delivery_id) if job.delivery_id else None
        if d:
            d.state, d.message = RUNNING, running_text(d.kind, 0, 0, "", Eta())
            self.history.save()
            card = self._card(d)
            if card:
                card.eta = Eta()
                card.render()

    def _on_progress(self, job: Job, done: int, total: int, msg: str) -> None:
        d = self.history.get(job.delivery_id) if job.delivery_id else None
        card = self._card(d) if d else None
        if card and d.state == RUNNING:
            card.progress(done, total, msg)

    def _on_finished(self, job: Job, result, exc: Optional[BaseException]) -> None:
        self.running = None
        if job.delivery_id:
            self.jobs.pop(job.delivery_id, None)
        if self.queue.empty():
            self.awake.stop()
        if job.kind == VERIFY:
            self._verified(job, result, exc)
            return
        d = self.history.get(job.delivery_id)
        if d is None:
            return
        d.details = "\n".join(job.messages[-30:])
        if exc is not None:
            ex = explain(exc, d.kind)
            d.state = PAUSED if isinstance(exc, Cancelled) else FAILED
            d.message, d.action = ex.message, ex.action
            if not isinstance(exc, Cancelled):
                d.details = (d.details + "\n\n" + ex.details).strip()
        elif d.kind == SEND:
            d.state, d.action, d.finished = DONE, None, now_iso()
            d.result, d.size, d.files = str(result.target), result.source_bytes, len(result.files)
            d.hint = SENT_HINT
            desktop.notify(APP_NAME, f"„{d.name}“ liegt in Google Drive.")
        else:
            d.result, d.sender = str(result.dest), result.sender or d.sender
            d.size, d.files = result.bytes or d.size, result.files
            notes = []
            if result.repaired_stripes:
                notes.append("Beschädigte Teile wurden automatisch repariert.")
            if result.renamed:
                notes.append(f"{len(result.renamed)} Dateiname(n) wurden für diesen Computer angepasst.")
            if result.without_manifest:
                notes.append("Ohne Inhaltsliste wiederhergestellt – ob etwas fehlt, lässt sich nicht prüfen.")
            d.hint = " ".join(notes)
            if result.complete:
                d.state, d.action, d.finished = DONE, "reveal", now_iso()
                desktop.notify(APP_NAME, f"„{d.name}“ ist angekommen.")
            else:
                n = len(result.missing)
                d.state, d.action = INCOMPLETE, "fetch_missing"
                d.message = ("1 Datei fehlt noch" if n == 1 else f"{n} Dateien fehlen noch") + \
                    " – bitte auf dem sendenden Mac noch einmal senden."
                desktop.notify(APP_NAME, f"„{d.name}“: Es fehlen noch Dateien.")
        self.history.save()
        self._render(d)

    # ------------------------------------------------------------ verify (settings)
    def start_verify(self, folder: Path) -> None:
        options = {} if read_manifest(folder) else {"without_manifest": True}
        self.submit(Job(VERIFY, None, [str(folder)], None, folder.name, options))

    def _verified(self, job: Job, result, exc) -> None:
        from vidtransfer.messages import verify_summary

        self.root.title(APP_NAME)
        if exc is not None:
            if not isinstance(exc, Cancelled):
                messagebox.showerror(APP_NAME, explain(exc, RECEIVE).message)
            return
        show = messagebox.showinfo if result.ok else messagebox.showwarning
        show(APP_NAME, verify_summary(result))

    # ------------------------------------------------------------ settings window
    def open_settings(self) -> None:
        SettingsWindow(self)

    # ------------------------------------------------------------ closing
    def _on_close(self) -> None:
        if self.running is not None or not self.queue.empty():
            if not messagebox.askyesno(APP_NAME, "Es wird gerade gesendet oder empfangen. Trotzdem beenden?\n\n"
                                                 "Beim nächsten Start kann es dort weitergehen, wo es aufgehört hat."):
                return
            self.closing = True
            for job in list(self.jobs.values()):
                job.cancel.set()
            if self.running is not None:
                self.running.cancel.set()
            return
        self.awake.stop()
        self.root.destroy()


class SettingsWindow:
    """Rarely needed settings and the technical functions."""

    def __init__(self, app: App):
        self.app = app
        app._reload_config()
        cfg = app.cfg
        win = self.win = tk.Toplevel(app.root)
        win.title("Einstellungen")
        win.transient(app.root)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Name dieses Computers").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar(value=cfg.sender_name or "")
        name = ttk.Entry(f, textvariable=self.name_var, width=28)
        name.grid(row=0, column=1, sticky="we", padx=(12, 0))
        ttk.Label(f, text=f"Wird beim Empfänger angezeigt. Leer = „{desktop.computer_name()}“.",
                  style="Muted.TLabel").grid(row=1, column=1, sticky="w", padx=(12, 0))

        self.drive_var = tk.StringVar(value=cfg.drive_root or "(wird beim ersten Senden gesucht)")
        self._path_row(f, 2, "Senden nach (Google Drive)", self.drive_var, self._change_drive)
        self.inbox_var = tk.StringVar(value=str(app.inbox()))
        self._path_row(f, 3, "Empfangene Lieferungen in", self.inbox_var, self._change_inbox)

        ttk.Separator(f).grid(row=4, column=0, columnspan=3, sticky="we", pady=12)
        tools = ttk.Frame(f)
        tools.grid(row=5, column=0, columnspan=3, sticky="w")
        ttk.Button(tools, text="Lieferung prüfen …", command=self._verify).pack(side="left")
        ttk.Button(tools, text="Protokoll öffnen",
                   command=lambda: desktop.open_path(log_dir())).pack(side="left", padx=8)
        ttk.Button(tools, text="Erledigte aus der Liste entfernen", command=self._clean).pack(side="left")

        self.adv_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Erweitert", variable=self.adv_var, command=self._toggle_adv).grid(
            row=6, column=0, sticky="w", pady=(12, 0))
        self.adv = ttk.Frame(f)
        self.vars = {}
        for i, (key, label, lo, hi) in enumerate((
                ("k", "Datenpakete je Gruppe (k)", 1, 99), ("m", "Reservepakete je Gruppe (m)", 0, 50),
                ("block_mib", "Paketgröße in MiB", 1, 512), ("zstd_level", "Kompression (0 = aus)", 0, 19))):
            ttk.Label(self.adv, text=label).grid(row=i, column=0, sticky="w")
            var = tk.IntVar(value=getattr(cfg, key))
            ttk.Spinbox(self.adv, from_=lo, to=hi, textvariable=var, width=6).grid(row=i, column=1, sticky="w", padx=8)
            self.vars[key] = var
        ttk.Label(self.adv, text="Nur ändern, wenn nötig. Gilt für neue Lieferungen.",
                  style="Muted.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        bottom = ttk.Frame(f)
        bottom.grid(row=8, column=0, columnspan=3, sticky="we", pady=(16, 0))
        ttk.Label(bottom, text=f"Version {__version__}", style="Muted.TLabel").pack(side="left")
        ttk.Button(bottom, text="Fertig", command=self.close).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", self.close)

    def _path_row(self, f, row, label, var, command) -> None:
        ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=(10, 0))
        ttk.Label(f, textvariable=var, style="Muted.TLabel").grid(row=row, column=1, sticky="w",
                                                                   padx=(12, 0), pady=(10, 0))
        ttk.Button(f, text="Ändern …", command=command).grid(row=row, column=2, sticky="e", pady=(10, 0))

    def _toggle_adv(self) -> None:
        if self.adv_var.get():
            self.adv.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
        else:
            self.adv.grid_remove()

    def _change_drive(self) -> None:
        chosen = self.app._drive_root(ask=True)
        if chosen:
            self.drive_var.set(str(chosen))

    def _change_inbox(self) -> None:
        new = self.app._ask_dir("Wo sollen empfangene Lieferungen gespeichert werden?", str(self.app.inbox()))
        if new:
            self.app.cfg.update(receive_dir=str(new))
            self.inbox_var.set(str(new))
            self.app._show_inbox()

    def _verify(self) -> None:
        p = self.app._ask_dir("Lieferung zum Prüfen wählen", self.app.cfg.last_transfer_dir,
                              self.app.cfg.drive_root, str(default_cloudstorage_dir()))
        if p:
            self.app.start_verify(p)

    def _clean(self) -> None:
        self.app.history.remove_finished()
        self.app.refresh()

    def close(self) -> None:
        from vidtransfer.sender import MIB, SendOptions

        changes = {"sender_name": self.name_var.get().strip() or None}
        try:
            values = {k: int(v.get()) for k, v in self.vars.items()}
            SendOptions(k=values["k"], m=values["m"], block_size=values["block_mib"] * MIB,
                        zstd_level=values["zstd_level"]).validate()
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Einstellungen", f"Ungültige Werte unter „Erweitert“: {exc}", parent=self.win)
            return
        changes.update(values)
        self.app.cfg.update(**changes)
        self.win.destroy()


def run_gui(paths: Optional[list[str]] = None) -> int:
    root = tk.Tk()
    App(root, list(paths or []))
    root.mainloop()
    return 0
