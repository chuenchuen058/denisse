"""A reply must never land nowhere.

The bug this pins down: when no "thinking" bubble is up, _show_reply_main
makes a fresh ThinkingBubble and schedules deliver() for TYPING_MS later,
so it types for a beat before the message lands. Anything that calls
_close_reply_bubble() inside that pause destroys the bubble the reply was
going to land in -- and there are several real callers:

  * _show_reminder_card(), on the 15-minute loop and on every unlock/wake
  * _show_study_bubble(), when a pomodoro phase turns over
  * _set_thinking_main(), when a second question starts its own bubble
    (ask by voice, then start typing before her answer renders)

ThinkingBubble.deliver() then raised tk.TclError on a destroyed window and
swallowed it as "closed before the reply came". With the chat popup shut
and TTS off, that is the entire visible answer to a "hey Denisse" gone.

Run after touching _show_reply_main, _deliver_reply or ThinkingBubble.
Needs a desktop session -- it drives real Tk windows.
"""
import os
import sys
import time
import tkinter as tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import reminder_card  # noqa: E402
import tray  # noqa: E402

PAUSE = reminder_card.TYPING_MS


class StubOrb:
    """Enough of a FloatingOrb for the bubble to lay itself out. _orb_box
    catches the missing .win and falls back to the screen corner, which is
    all this test needs -- it is about delivery, not placement."""
    _frame_i = 0
    total_px = 64


def pump(ms):
    """Let Tk run its event loop for ms, the way the tray's mainloop would."""
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        tray.root.update()
        time.sleep(0.01)


def reset():
    tray._close_reply_bubble()
    tray._close_thinking_bubble()
    pump(120)
    tray._reply_bubble = None
    tray._thinking_bubble = None


def landed(text):
    """Is the reply actually on screen?"""
    b = tray._reply_bubble
    if b is None:
        return False
    try:
        if not b.win.winfo_exists():
            return False
    except tk.TclError:
        return False
    return getattr(b, "message", None) == text


results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


tray.root = tk.Tk()
tray.root.withdraw()
tray._floating_orb = StubOrb()

# 1. The plain path still works: no interruption, reply lands after the pause.
reset()
tray._show_reply_main("plain reply")
pump(PAUSE + 500)
check("uninterrupted reply lands", landed("plain reply"),
      f"message={getattr(tray._reply_bubble, 'message', None)!r}")

# 2. THE BUG. The bubble is closed during the typing pause, exactly as the
#    reminder card or a second question would close it.
reset()
tray._show_reply_main("interrupted reply")
pump(60)
tray._close_reply_bubble()      # what _show_reminder_card() does
pump(PAUSE + 700)
check("reply survives being closed mid-pause", landed("interrupted reply"),
      f"message={getattr(tray._reply_bubble, 'message', None)!r}")

# 3. The handover path: a "thinking" bubble is up and becomes the reply.
reset()
tray._set_thinking_main("chat", True)
pump(150)
had_thinking = tray._thinking_bubble is not None
tray._show_reply_main("handed over")
pump(400)
check("thinking bubble becomes the reply", had_thinking and landed("handed over"),
      f"had_thinking={had_thinking} message={getattr(tray._reply_bubble, 'message', None)!r}")

# 4. A dead thinking bubble must not swallow the reply either.
reset()
tray._set_thinking_main("chat", True)
pump(150)
if tray._thinking_bubble is not None:
    tray._thinking_bubble.win.destroy()   # window gone, global still set
tray._show_reply_main("after a dead bubble")
pump(PAUSE + 700)
check("dead thinking bubble does not eat the reply", landed("after a dead bubble"),
      f"message={getattr(tray._reply_bubble, 'message', None)!r}")

reset()
try:
    tray.root.destroy()
except tk.TclError:
    pass

bad = [n for n, ok, _ in results if not ok]
print()
print(f"{len(results) - len(bad)}/{len(results)} passed")
sys.exit(1 if bad else 0)
