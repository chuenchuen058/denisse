"""
Denisse -- what a timers.py countdown looks like on screen.

While a timer runs, a pill under the orb counts it down ("oven  4:12"),
the same pill study mode uses but amber, so the two can't be mistaken for
each other. Chuen's first complaint about study mode was "the timer is not
showing" (2026-10-19), so a timer with nothing on screen would repeat it.
When one finishes: a chime, and a message from the orb the way her replies
arrive.

timers.py runs its clock on its own thread and knows nothing about Tk;
everything here runs on the Tk thread, reached through root.after.
"""

import tkinter as tk
from datetime import datetime

import reminders_config as _cfg
import timers
from reminder_card import WARN
from study_mode import CHECK_MS, StudyBadge, _chime


class TimerBadge(StudyBadge):
    """StudyBadge with its own colour and text -- placement, the pill
    drawing and keeping clear of bubbles are all inherited."""

    def update(self, label, ends_at, total_s=0):
        try:
            left = max(0, (ends_at - datetime.now()).total_seconds())
            if self._color != WARN:
                self._color = WARN
                self.canvas.itemconfig(self._img_id, image=self._pill(WARN))
            m, sec = divmod(int(left + 0.999), 60)
            h, m = divmod(m, 60)
            # Wording and the label's length are in reminders.json, "timer_ui".
            clock = (_cfg.say("timer_ui.clock_hours", h=h, m=m, s=sec) if h
                     else _cfg.say("timer_ui.clock_minutes", m=m, s=sec))
            name = (label or _cfg.get("timer_ui.unnamed"))[:_cfg.get("timer_ui.label_chars")]
            self.canvas.itemconfig(self._text, text=_cfg.say("timer_ui.badge", name=name, clock=clock))
            self._follow()
        except tk.TclError:
            pass


class TimerDisplay:
    """Owns the pill. `say(text)` is tray.py's reply bubble (main thread);
    `notify(title, text)` is the tray-icon fallback when there's no orb."""

    def __init__(self, root, orb, say, notify=None, font_family=None, avoid=None):
        self.root, self.orb, self.say, self.notify = root, orb, say, notify
        self.font, self.avoid = font_family or _cfg.get("card.font_family"), avoid
        self.badge = None
        self._job = None
        timers.set_notifier(lambda t: root.after(0, lambda: self._finished(t)))
        timers.add_listener(lambda: root.after(0, self._sync))

    def _sync(self):
        running = timers.active()
        if not running:
            if self.badge is not None:
                self.badge.close()
                self.badge = None
            return
        if self.badge is None and self.orb is not None:
            try:
                self.badge = TimerBadge(self.root, self.orb, self._status,
                                        font_family=self.font, avoid=self.avoid)
            except Exception:
                self.badge = None  # cosmetic -- the timer itself still fires
        if self._job is None:
            self._tick()

    def _tick(self):
        self._job = None
        running = timers.active()
        if not running:
            self._sync()
            return
        if self.badge is not None:
            soonest = running[0]
            self.badge.update(soonest["label"], soonest["ends_at"])
        self._job = self.root.after(CHECK_MS, self._tick)

    def _status(self):
        self.say(timers.list_timers())

    def _finished(self, t):
        _chime()
        line, what, notify_app = finished_lines(t)
        if self.orb is None and self.notify is not None:
            self.notify(_cfg.say("timer_ui.notify_title", what=what), notify_app)
        else:
            self.say(line)
        self._sync()


def finished_lines(t):
    """(what she says, what the tray notice calls it, the notice's sender)
    for a finished timer -- all from reminders.json, "timer_ui"."""
    if t["label"]:
        what = _cfg.say("timer_ui.what_named", label=t["label"])
        line = _cfg.say("timer_ui.done_named", what=what)
    else:
        what = _cfg.say("timer_ui.what_unnamed")
        line = _cfg.say("timer_ui.done_unnamed")
    return line, what, _cfg.get("timer_ui.notify_app")
