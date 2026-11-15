"""No console windows for the child processes Denisse's background jobs start (Windows).

One helper, used by every background entry point (the Orchestra runner, the Atrium live page
and digest, the nightly vault job, the scheduled eval) instead of a copy per file:

    import no_window; no_window.install()      # every later subprocess.Popen in this process
    subprocess.run(argv, creationflags=no_window.flags())   # or one call, explicitly

Why (Chuen, 2 Nov 2026: "the claude terminal keeps spamming my entire computer"; 15 Nov 2026:
"I keep getting kicked out" of a full-screen game): a pythonw process has no console, so each
console program it starts (git, python, powershell) opens a window of its own, which flashes
and takes the focus. CREATE_NO_WINDOW gives the child a hidden console that its own children
inherit. A caller that asks for a console on purpose (CREATE_NEW_CONSOLE, DETACHED_PROCESS:
Mission Control, the live lane window) is left alone. Off Windows both are no-ops.
"""
from __future__ import annotations

import os
import subprocess

CREATE_NO_WINDOW = 0x08000000
# DETACHED_PROCESS | CREATE_NEW_CONSOLE: the caller chose its console; don't override that.
OWN_CONSOLE = 0x00000008 | 0x00000010
MARK = "_orchestra_quiet"  # the attribute that says a Popen.__init__ is already wrapped


def flags(creationflags: int = 0, windows: bool | None = None) -> int:
    """`creationflags` plus CREATE_NO_WINDOW, unless the caller asked for its own console."""
    windows = os.name == "nt" if windows is None else windows
    creationflags = int(creationflags or 0)
    if not windows or creationflags & OWN_CONSOLE:
        return creationflags
    return creationflags | CREATE_NO_WINDOW


def install(cls=None, windows: bool | None = None) -> bool:
    """Wrap cls.__init__ (subprocess.Popen) so every child gets flags(). Idempotent: a second
    call, or a module reload, never wraps the wrapper. True when the wrapper is in place."""
    windows = os.name == "nt" if windows is None else windows
    cls = subprocess.Popen if cls is None else cls
    if not windows:
        return False
    if getattr(cls.__init__, MARK, False):
        return True
    original = cls.__init__

    def quiet_init(self, *args, **kwargs):
        kwargs["creationflags"] = flags(kwargs.get("creationflags", 0), windows=True)
        original(self, *args, **kwargs)

    setattr(quiet_init, MARK, True)
    cls.__init__ = quiet_init
    return True
