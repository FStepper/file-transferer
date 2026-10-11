"""Shared test helpers."""

import tkinter as tk

_root = None


def new_root() -> tk.Toplevel:
    """A hidden window for one test's App.

    All tests share one Tk interpreter: on macOS a second tk.Tk() in the same
    process hangs in update(), and on Windows it sporadically fails to read
    init.tcl. Each test gets its own Toplevel instead and destroys it.
    """
    global _root
    if _root is None:
        _root = tk.Tk()
        _root.withdraw()
    win = tk.Toplevel(_root)
    win.withdraw()
    return win
