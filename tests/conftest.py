"""Shared test helpers.

Window tests (marker "gui") need a Tk interpreter, and Tk is picky about that:
- Windows: creating many tk.Tk() in one process sporadically fails to read
  init.tcl, so all window tests share one root and each gets a Toplevel.
- macOS: a second tk.Tk() in a process hangs in update(), and so does a
  Toplevel of a withdrawn shared root. Only the first tk.Tk() of a process
  works reliably, so CI runs each window test in its own process there
  (see .github/workflows) and new_root() returns a plain tk.Tk().
"""

import sys
import tkinter as tk

import pytest

IS_MAC = sys.platform == "darwin"
_root = None


def new_root():
    """A hidden window for one test's App (tk.Tk on macOS, else a Toplevel)."""
    global _root
    if IS_MAC:
        root = tk.Tk()
        root.withdraw()
        return root
    if _root is None:
        _root = tk.Tk()
        _root.withdraw()
    win = tk.Toplevel(_root)
    win.withdraw()
    return win


def pytest_collection_modifyitems(items):
    # Everything using the window fixture of test_settings is a window test
    for item in items:
        if item.module.__name__ == "test_settings" and "app" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.gui)
