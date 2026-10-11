"""Shared test helpers."""

import time
import tkinter as tk


def new_root() -> tk.Tk:
    """A hidden Tk root window.

    On Windows, creating many Tcl interpreters in one process sporadically fails
    with "couldn't read file .../init.tcl: No error" (seen on GitHub runners and
    locally). The file is fine; a second attempt succeeds, so retry a few times.
    """
    for attempt in range(5):
        try:
            root = tk.Tk()
            root.withdraw()
            return root
        except tk.TclError as exc:
            if "init.tcl" not in str(exc) or attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise AssertionError("unreachable")
