"""Main window (Tk; UI language: German).

One window, two modes – "Senden" and "Empfangen" – switched at the top; the
last mode is remembered.
- "Senden": one drop zone and the list of sent deliveries.
- "Empfangen": new deliveries in the Google Drive folder are received
  automatically into the inbox; the list shows them with the sender's name.
Each delivery card has one status line and at most one button. Everything
technical (log, manual verify, parameters) lives in the settings window or
behind "Details".

Threading: one worker thread runs the jobs one after another, one watcher
thread scans the Drive folder; both only post events. All widgets and the
delivery list are changed on the Tk main thread.
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

from vidtransfer import APP_NAME, __version__, desktop, watch
from vidtransfer.config import Config, log_dir
from vidtransfer.drive import default_cloudstorage_dir, find_drive_roots
from vidtransfer.friendly import ACTIONS, Eta, explain, running_text, size_line, when
from vidtransfer.history import (
    ACTIVE, DONE, FAILED, INCOMPLETE, OFFERED, PAUSED, QUEUED, RUNNING, Delivery, History, Seen, now_iso,
)
from vidtransfer.manifest import is_transfer_folder, read_manifest
from vidtransfer.util import Cancelled, human_bytes

log = logging.getLogger(__name__)
IS_MAC, IS_WIN = desktop.IS_MAC, desktop.IS_WIN
SEND, RECEIVE, VERIFY = "send", "receive", "verify"
SCAN_SECONDS = 20

SENT_HINT = "Google Drive lädt jetzt im Hintergrund hoch – bitte online bleiben, bis Google Drive fertig ist."
DELETED_HINT = ("In Google Drive gelöscht. Der Platz wird frei, sobald der Papierkorb von "
                "Google Drive geleert ist.")


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
    """Dashed area: drop files/folders onto it, or click it / its button."""

    def __init__(self, parent, text: str, button_text: str, on_click: Callable[[], None],
                 drop_enabled: bool, bg: str):
        super().__init__(parent, height=96, highlightthickness=0, bd=0, bg=bg)
        # Without tkdnd only the Dock icon accepts drops, so do not promise it here
        self.text = text if drop_enabled else text.replace("hierher ziehen", "auswählen")
        self._hover = False
        self.button = ttk.Button(self, text=button_text, command=on_click)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: on_click())

    def set_hover(self, value: bool) -> None:
        self._hover = value
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        color = "#3b82f6" if self._hover else "gray55"
        self.create_rectangle(3, 3, w - 3, h - 3, dash=(6, 4), outline=color, width=2)
        self.create_text(w / 2, h / 2 - 16, text=self.text, font=App.fonts["zone"], fill=color)
        self.create_window(w / 2, h / 2 + 16, window=self.button)


class Card(ttk.Frame):
    """One delivery: name, size, progress bar (while running), status, one button."""

    def __init__(self, parent, app: "App", d: Delivery):
        super().__init__(parent, padding=(2, 7, 2, 7))
        self.app = app
        self.d = d
        self.eta = Eta()
        self.incoming: Optional[int] = None      # % of the delivery already in the Drive folder
        self._waiting = False
        self.columnconfigure(0, weight=1)
        self.title = ttk.Label(self, style="CardTitle.TLabel")
        self.meta = ttk.Label(self, style="Muted.TLabel")
        self.bar = ttk.Progressbar(self, mode="determinate", maximum=1000)
        self.status = ttk.Label(self, wraplength=430, justify="left")
        self.hint = ttk.Label(self, style="Muted.TLabel", wraplength=430, justify="left")
        self.button = ttk.Button(self, command=self._on_button)
        self.link = ttk.Label(self, style="Link.TLabel", cursor="hand2")
        self.link.bind("<Button-1>", self._on_link)
        self.title.grid(row=0, column=0, sticky="w")
        self.meta.grid(row=1, column=0, sticky="w")
        self.status.grid(row=3, column=0, sticky="w", pady=(3, 0))
        for w in (self, self.title, self.meta, self.status, self.hint):
            w.bind("<Button-2>" if IS_MAC else "<Button-3>", self._menu)
        self.bind("<Configure>", lambda e: self._rewrap())
        self.render()

    def _rewrap(self) -> None:
        """Wrap long status lines at the card width minus the button."""
        width = self.winfo_width()
        if width <= 1:
            return
        button = self.button.winfo_reqwidth() + 12 if self.button.winfo_manager() else 0
        self.status.configure(wraplength=max(160, width - button - 8))
        self.hint.configure(wraplength=max(160, width - 8))

    def _meta_text(self) -> str:
        return size_line(self.d.size, self.d.files, self.d.sender if self.d.kind == RECEIVE else None)

    def render(self) -> None:
        d = self.d
        self.title.configure(text=d.name)
        self.meta.configure(text=self._meta_text())
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
            self.bar.grid(row=2, column=0, columnspan=2, sticky="we", pady=(5, 0))
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
        link = self._link_text()
        if link:
            self.link.configure(text=link)
            self.link.grid(row=5, column=0, sticky="w")
        else:
            self.link.grid_remove()
        self._rewrap()

    def _link_text(self) -> str:
        d = self.d
        if d.state in (FAILED, INCOMPLETE) and d.details:
            return "Details"
        if self.app.can_free_drive(d):
            return f"Platz in Google Drive freigeben ({human_bytes(d.size)})" if d.size else \
                "Platz in Google Drive freigeben"
        return ""

    def _on_link(self, _event) -> None:
        if self.d.state in (FAILED, INCOMPLETE):
            self.app.show_details(self.d)
        else:
            self.app.free_drive(self.d)

    def progress(self, done: int, total: int, msg: str) -> None:
        self._waiting = msg.startswith("Warte")
        if total:
            self.bar.configure(value=1000 * min(done, total) / total)
            if not self.d.size:
                self.d.size = total
                self.meta.configure(text=self._meta_text())
        self.status.configure(text=running_text(self.d.kind, done, total, msg, self.eta, self.incoming))

    def set_incoming(self, percent: int) -> None:
        self.incoming = percent
        if self.d.state == RUNNING and self._waiting:
            self.status.configure(text=running_text(self.d.kind, 0, 0, "Warte", self.eta, percent))

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
            self.empty.grid(row=0, column=0, sticky="w", pady=10)
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
        self.seen = Seen(self.cfg.seen_path())
        self.queue: "queue.Queue[Job]" = queue.Queue()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.jobs: dict[str, Job] = {}          # delivery id -> job (queued or running)
        self.running: Optional[Job] = None
        self.closing = False
        self.awake = desktop.KeepAwake()
        self.drop_enabled = desktop.enable_drop(root)
        self.found: dict[str, watch.Found] = {}   # delivery folder -> last scan result
        self._watch_root: Optional[Path] = None
        self._watch_wakeup = threading.Event()

        self._styles()
        self._build()
        threading.Thread(target=self._worker, name="vt-worker", daemon=True).start()
        threading.Thread(target=self._watcher, name="vt-watch", daemon=True).start()
        self._update_watch()
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
        family = base.cget("family")
        App.fonts = {"card": (family, size, "bold"), "zone": (family, size + 1)}
        style = ttk.Style(self.root)
        style.configure("CardTitle.TLabel", font=App.fonts["card"])
        style.configure("Muted.TLabel", foreground="gray50")
        style.configure("Link.TLabel", foreground="#3b82f6")
        # Mode switch: a framed pair of toolbuttons, so both halves look clickable
        style.configure("Segment.TFrame", relief="solid", borderwidth=1)
        style.configure("Toolbutton", padding=(14, 3))
        self.bg = style.lookup("TFrame", "background") or self.root.cget("background")

    def _build(self) -> None:
        r = self.root
        r.title(APP_NAME)
        r.geometry("540x470")
        r.minsize(440, 340)
        main = ttk.Frame(r, padding=(12, 10, 12, 8))
        main.pack(fill="both", expand=True)

        head = ttk.Frame(main)
        head.pack(fill="x")
        switch = ttk.Frame(head, style="Segment.TFrame", padding=2)
        switch.pack(side="left")
        self.mode_var = tk.StringVar(value=self.cfg.mode if self.cfg.mode in (SEND, RECEIVE) else SEND)
        for value, text in ((SEND, "Senden"), (RECEIVE, "Empfangen")):
            ttk.Radiobutton(switch, text=text, value=value, variable=self.mode_var, style="Toolbutton",
                            command=self._mode_changed).pack(side="left")
        ttk.Button(head, text="Einstellungen", command=self.open_settings).pack(side="right")

        self.pages = {}
        send = ttk.Frame(main)
        self.send_zone = DropZone(send, "Ordner mit Videos hierher ziehen", "Ordner auswählen …",
                                  self._pick_send, self.drop_enabled, self.bg)
        self.send_zone.pack(fill="x", pady=(10, 6))
        self.send_list = DeliveryList(send, self, SEND, "Noch nichts gesendet.", self.bg)
        self.send_list.pack(fill="both", expand=True)
        self.pages[SEND] = send

        recv = ttk.Frame(main)
        top = ttk.Frame(recv)
        top.pack(fill="x", pady=(10, 6))
        top.columnconfigure(0, weight=1)
        self.recv_status = ttk.Label(top, wraplength=380, justify="left")
        self.recv_status.grid(row=0, column=0, sticky="w")
        self.recv_where = ttk.Label(top, style="Muted.TLabel", wraplength=380, justify="left")
        self.recv_where.grid(row=1, column=0, sticky="w")
        self.recv_button = ttk.Button(top, text="Lieferung öffnen …", command=self._pick_delivery)
        self.recv_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(8, 0))
        ttk.Separator(recv).pack(fill="x")
        self.recv_list = DeliveryList(recv, self, RECEIVE, "Noch nichts empfangen.", self.bg)
        self.recv_list.pack(fill="both", expand=True)
        self.pages[RECEIVE] = recv

        if self.drop_enabled:
            self._drop_target(self.send_zone, highlight=self.send_zone)
            self._drop_target(self.send_list.canvas)
            for w in (recv, top, self.recv_status, self.recv_where, self.recv_list.canvas):
                self._drop_target(w)

        self._show_receive_header()
        self._show_page()
        self.refresh()

    def _drop_target(self, widget, highlight: Optional[DropZone] = None) -> None:
        """Accept dropped files/folders on `widget` (requires tkdnd)."""
        from tkinterdnd2 import DND_FILES

        def drop(event):
            if highlight:
                highlight.set_hover(False)
            self.handle_paths(list(widget.tk.splitlist(event.data)))
            return event.action

        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", drop)
        if highlight:
            widget.dnd_bind("<<DropEnter>>", lambda e: highlight.set_hover(True) or e.action)
            widget.dnd_bind("<<DropLeave>>", lambda e: highlight.set_hover(False) or e.action)

    def _show_page(self) -> None:
        for mode, page in self.pages.items():
            if mode == self.mode_var.get():
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()

    def _mode_changed(self) -> None:
        self._show_page()
        self.cfg.update(mode=self.mode_var.get())
        self._update_watch()

    def set_mode(self, mode: str) -> None:
        if self.mode_var.get() != mode:
            self.mode_var.set(mode)
            self._mode_changed()

    def refresh(self) -> None:
        self.send_list.rebuild(self.history.of_kind(SEND))
        self.recv_list.rebuild(self.history.of_kind(RECEIVE))
        for folder, found in self.found.items():
            self._apply_incoming(found)

    def _card(self, d: Delivery) -> Optional[Card]:
        lst = self.send_list if d.kind == SEND else self.recv_list
        return lst.cards.get(d.id)

    def _render(self, d: Delivery) -> None:
        card = self._card(d)
        if card:
            card.render()

    def _show_receive_header(self) -> None:
        drive = self._watch_folder()
        if not self.cfg.auto_receive:
            self.recv_status.configure(text="Automatisches Empfangen ist ausgeschaltet (Einstellungen).")
        elif drive is None:
            self.recv_status.configure(text="Bitte zuerst den Google-Drive-Ordner wählen, damit neue "
                                            "Lieferungen automatisch empfangen werden.")
        else:
            self.recv_status.configure(text="Neue Lieferungen werden automatisch empfangen.")
        self.recv_where.configure(text=f"Ablage: {self.inbox()}")
        if self.cfg.auto_receive and drive is None:
            self.recv_button.configure(text=ACTIONS["choose_drive"], command=self._choose_drive_for_watch)
        else:
            self.recv_button.configure(text="Lieferung öffnen …", command=self._pick_delivery)

    def _choose_drive_for_watch(self) -> None:
        if self._drive_root(ask=True):
            self._update_watch()

    # ------------------------------------------------------------ settings helpers
    def _reload_config(self) -> None:
        """Pick up settings another app window may have changed."""
        self.cfg = Config.load()
        self._show_receive_header()

    def inbox(self) -> Path:
        return Path(self.cfg.receive_dir) if self.cfg.receive_dir else desktop.default_inbox()

    def sender_name(self) -> str:
        return self.cfg.sender_name or desktop.computer_name()

    def _watch_folder(self) -> Optional[Path]:
        """The Drive folder to watch: the configured one, or the only Drive found."""
        if self.cfg.drive_root:
            return Path(self.cfg.drive_root) if Path(self.cfg.drive_root).is_dir() else None
        roots = find_drive_roots()
        return roots[0] if len(roots) == 1 else None

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
        """Google Drive folder: the saved one; otherwise the only Drive found; otherwise ask.

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
            chosen = self._ask_dir("Google-Drive-Ordner für die Lieferungen wählen",
                                   cur, *(str(r) for r in roots), str(default_cloudstorage_dir()))
            if chosen is None:
                return None
        self.cfg.update(drive_root=str(chosen))
        self._show_receive_header()
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

    def start_receive(self, folder: Path, auto: bool = False, offered: Optional[Delivery] = None) -> None:
        srcs = [str(folder)]
        if self._busy_with(RECEIVE, srcs):
            return
        if offered is None:                       # dropped by hand while it is offered: reuse that card
            offered = next((d for d in self.history.of_kind(RECEIVE)
                            if d.sources == srcs and d.state == OFFERED), None)
        options: dict = {"auto": True} if auto else {}
        man = read_manifest(folder)
        if man is None:
            if auto or not messagebox.askyesno(APP_NAME, (
                    f"Von „{folder.name}“ fehlt noch die Inhaltsliste.\n\n"
                    "Meist ist die Lieferung einfach noch nicht ganz in Google Drive angekommen – "
                    "dann bitte etwas warten.\n\nSoll trotzdem versucht werden, die Dateien "
                    "wiederherzustellen (Notfall)?")):
                return
            options["without_manifest"] = True
        man = man or {}
        if man.get("transfer_id"):
            self.seen.add(man["transfer_id"])
        inbox = self.inbox()
        inbox.mkdir(parents=True, exist_ok=True)
        if offered is not None:                   # an offered card becomes the receive card
            d = offered
            d.target, d.options = str(inbox), options
        else:
            d = Delivery(RECEIVE, man.get("name", folder.name), srcs, str(inbox), options=options,
                         sender=man.get("sender"), size=man.get("source_bytes", 0),
                         files=len(man.get("files", [])), transfer_id=man.get("transfer_id"))
            self.history.add(d)
        d.state, d.message, d.action, d.hint = QUEUED, "Wartet …", None, ""
        self.history.save()
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
        if action == "receive_now":
            self.start_receive(Path(d.sources[0]), offered=d)
            return
        if action == "choose_inbox":
            new = self._ask_dir("Wo sollen empfangene Lieferungen gespeichert werden?", str(self.inbox()))
            if not new:
                return
            self.cfg.update(receive_dir=str(new))
            self._show_receive_header()
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

    def can_free_drive(self, d: Delivery) -> bool:
        return (d.kind == RECEIVE and d.state == DONE and bool(d.transfer_id)
                and not d.options.get("deleted") and Path(d.sources[0]).is_dir())

    def free_drive(self, d: Delivery) -> None:
        size = f" ({human_bytes(d.size)})" if d.size else ""
        if not messagebox.askyesno(APP_NAME, (
                f"„{d.name}“{size} aus Google Drive löschen?\n\n"
                "Die empfangenen Dateien auf diesem Computer bleiben erhalten. Google Drive gibt "
                "den Platz frei, sobald auch sein Papierkorb geleert ist.")):
            return
        try:
            watch.delete_delivery(Path(d.sources[0]), d.transfer_id)
        except (watch.NotADeliveryError, OSError) as exc:
            log.warning("Freeing Drive space failed: %s", exc)
            messagebox.showwarning(APP_NAME, "Der Ordner in Google Drive enthält auch andere Dateien "
                                             "oder ist nicht erreichbar und wurde deshalb nicht gelöscht.")
            return
        d.options["deleted"] = True
        d.hint = DELETED_HINT
        self.history.save()
        self._render(d)

    def show_details(self, d: Delivery) -> None:
        win = tk.Toplevel(self.root)
        win.title(f"Details – {d.name}")
        win.geometry("600x340")
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

    # ------------------------------------------------------------ watching the Drive folder
    def _update_watch(self) -> None:
        """Watch the Drive folder only in mode "Empfangen" with automatic receiving on."""
        self._show_receive_header()
        folder = self._watch_folder() if self.cfg.auto_receive and self.mode_var.get() == RECEIVE else None
        self._watch_root = folder
        if folder:
            self._watch_wakeup.set()

    def _watcher(self) -> None:
        while True:
            self._watch_wakeup.wait(SCAN_SECONDS)
            self._watch_wakeup.clear()
            root = self._watch_root
            if root is None:
                continue
            try:
                results = watch.scan(root)
            except Exception:  # noqa: BLE001 – try again with the next scan
                log.exception("Scanning %s failed", root)
                continue
            self.events.put(("scan", root, results))

    def _on_scan(self, root: Path, results: list[watch.Found]) -> None:
        if root != self._watch_root:
            return                                 # mode or folder changed meanwhile
        self.found = {str(f.folder): f for f in results}
        first_time = not self.cfg.auto_initialized
        if first_time:
            self.cfg.update(auto_initialized=True)
        own = {d.transfer_id for d in self.history.of_kind(SEND) if d.transfer_id}
        known = {d.transfer_id for d in self.history.of_kind(RECEIVE) if d.transfer_id}
        changed = False
        for f in results:
            self._apply_incoming(f)
            self._maybe_complete(f)
            if f.transfer_id in self.seen or f.transfer_id in known:
                continue
            if first_time or f.transfer_id in own:
                # Already there before automatic receiving was used, or sent from this
                # computer: offer it instead of fetching it unasked
                self.seen.add(f.transfer_id)
                self.history.add(Delivery(
                    RECEIVE, f.name, [str(f.folder)], None, state=OFFERED, sender=f.sender, size=f.size,
                    files=f.files, transfer_id=f.transfer_id, action="receive_now",
                    message=("Von diesem Computer gesendet – nicht automatisch empfangen."
                             if f.transfer_id in own else "Lag schon vorher in Google Drive – noch nicht empfangen.")))
                changed = True
                continue
            log.info("New delivery %s from %s - receiving automatically", f.name, f.sender)
            self.start_receive(f.folder, auto=True)
        if changed:
            self.refresh()

    def _apply_incoming(self, f: watch.Found) -> None:
        """Show how much of a delivery has arrived in Drive (receive cards that are waiting)."""
        for d in self.history.of_kind(RECEIVE):
            if d.sources == [str(f.folder)]:
                card = self._card(d)
                if card and d.state == RUNNING:
                    card.set_incoming(f.percent)
                elif card and d.state == OFFERED and not f.complete:
                    card.status.configure(text=f"Wird noch hochgeladen ({f.percent} %) – noch nicht empfangen.")

    def _maybe_complete(self, f: watch.Found) -> None:
        """An incomplete delivery is fetched again as soon as more packets have arrived."""
        for d in self.history.of_kind(RECEIVE):
            if d.sources == [str(f.folder)] and d.state == INCOMPLETE and f.recoverable \
                    and f.present > d.options.get("packets_seen", -1):
                d.options["packets_seen"] = f.present
                log.info("%s: missing packets arrived - completing", d.name)
                self.run_action(d, "fetch_missing")

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

            options = {k: v for k, v in job.options.items() if k in ("salvage", "without_manifest", "only")}
            return receive_folder(Path(job.sources[0]), Path(job.target), progress, job.cancel, **options)
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
                f = self.found.get(d.sources[0])
                if f:
                    card.incoming = f.percent

    def _on_progress(self, job: Job, done: int, total: int, msg: str) -> None:
        d = self.history.get(job.delivery_id) if job.delivery_id else None
        card = self._card(d) if d else None
        if card and d.state == RUNNING:
            card.progress(done, total, msg)

    def _on_finished(self, job: Job, result, exc: Optional[BaseException]) -> None:
        from vidtransfer.receiver import StripeUnavailableError

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
            if (d.kind == RECEIVE and d.options.get("auto") and not d.options.get("salvage")
                    and isinstance(exc, StripeUnavailableError) and exc.upload_complete):
                # Automatic receiving: fetch everything that is there, complete it later
                log.info("%s: parts are missing for good - restoring everything else", d.name)
                self.run_action(d, "salvage")
                return
            ex = explain(exc, d.kind)
            d.state = PAUSED if isinstance(exc, Cancelled) else FAILED
            d.message, d.action = ex.message, ex.action
            if not isinstance(exc, Cancelled):
                d.details = (d.details + "\n\n" + ex.details).strip()
        elif d.kind == SEND:
            d.state, d.action, d.finished = DONE, None, now_iso()
            d.result, d.size, d.files = str(result.target), result.source_bytes, len(result.files)
            d.transfer_id = result.transfer_id
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
                found = self.found.get(d.sources[0])
                d.options["packets_seen"] = found.present if found else -1
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
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Name dieses Computers").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar(value=cfg.sender_name or "")
        ttk.Entry(f, textvariable=self.name_var, width=26).grid(row=0, column=1, sticky="we", padx=(12, 0))
        ttk.Label(f, text=f"Wird beim Empfänger angezeigt. Leer = „{desktop.computer_name()}“.",
                  style="Muted.TLabel").grid(row=1, column=1, sticky="w", padx=(12, 0))

        self.drive_var = tk.StringVar(value=cfg.drive_root or "(wird beim ersten Senden gesucht)")
        self._path_row(f, 2, "Google-Drive-Ordner", self.drive_var, self._change_drive)
        self.inbox_var = tk.StringVar(value=str(app.inbox()))
        self._path_row(f, 3, "Empfangene Lieferungen in", self.inbox_var, self._change_inbox)
        checks = ttk.Frame(f)
        checks.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.auto_var = tk.BooleanVar(value=cfg.auto_receive)
        ttk.Checkbutton(checks, text="Im Modus „Empfangen“ neue Lieferungen automatisch empfangen",
                        variable=self.auto_var).pack(anchor="w")
        self.login_var = tk.BooleanVar(value=desktop.launch_at_login_enabled())
        if IS_MAC:
            ttk.Checkbutton(checks, text="Beim Anmelden automatisch starten",
                            variable=self.login_var).pack(anchor="w")

        ttk.Separator(f).grid(row=5, column=0, columnspan=3, sticky="we", pady=10)
        tools = ttk.Frame(f)
        tools.grid(row=6, column=0, columnspan=3, sticky="w")
        ttk.Button(tools, text="Lieferung prüfen …", command=self._verify).pack(side="left")
        ttk.Button(tools, text="Protokoll öffnen",
                   command=lambda: desktop.open_path(log_dir())).pack(side="left", padx=6)
        ttk.Button(tools, text="Erledigte aus der Liste entfernen", command=self._clean).pack(side="left")

        self.adv_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Erweitert", variable=self.adv_var, command=self._toggle_adv).grid(
            row=7, column=0, sticky="w", pady=(10, 0))
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
        bottom.grid(row=9, column=0, columnspan=3, sticky="we", pady=(14, 0))
        ttk.Label(bottom, text=f"Version {__version__}", style="Muted.TLabel").pack(side="left")
        ttk.Button(bottom, text="Fertig", command=self.close).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", self.close)

    def _path_row(self, f, row, label, var, command) -> None:
        ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=(8, 0))
        ttk.Label(f, textvariable=var, style="Muted.TLabel").grid(row=row, column=1, sticky="w",
                                                                   padx=(12, 0), pady=(8, 0))
        ttk.Button(f, text="Ändern …", command=command).grid(row=row, column=2, sticky="e", pady=(8, 0))

    def _toggle_adv(self) -> None:
        if self.adv_var.get():
            self.adv.grid(row=8, column=0, columnspan=3, sticky="w", pady=(6, 0))
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
            self.app._show_receive_header()

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

        changes = {"sender_name": self.name_var.get().strip() or None, "auto_receive": self.auto_var.get()}
        try:
            values = {k: int(v.get()) for k, v in self.vars.items()}
            SendOptions(k=values["k"], m=values["m"], block_size=values["block_mib"] * MIB,
                        zstd_level=values["zstd_level"]).validate()
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Einstellungen", f"Ungültige Werte unter „Erweitert“: {exc}", parent=self.win)
            return
        changes.update(values)
        self.app.cfg.update(**changes)
        if IS_MAC and self.login_var.get() != desktop.launch_at_login_enabled():
            try:
                desktop.set_launch_at_login(self.login_var.get())
            except OSError:
                log.exception("Changing start at login failed")
        self.app._update_watch()
        self.win.destroy()


def run_gui(paths: Optional[list[str]] = None) -> int:
    root = tk.Tk()
    App(root, list(paths or []))
    root.mainloop()
    return 0
