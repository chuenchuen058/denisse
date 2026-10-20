"""
Denisse -- plain countdown timers: "start a timer", "10 minute timer",
"remind me in 5 minutes to check the oven", "stop the timer".

Why this exists: on 2026-10-20 Chuen typed "start timer" and she had no
timer at all. The model improvised with run_shell_command("start") -- which
on Windows just opens an empty console -- and then said "The timer has
started." Then "stop timer" got "Done." with no tool call. Improvising is
only as good as the tools there are to improvise with, so this is the tool.

Study mode (study_mode.py) is the 25/5 pomodoro and stays separate: it is
matched before the model is ever called. These timers are the general case
and go through the model, so any wording works.

No Tk in here. The clock is one daemon thread checking wall-clock deadlines
every half second -- the same reason study_mode doesn't trust one long
after(): a laptop that sleeps past a deadline should fire once when it
wakes, not hours of drift. What happens on screen when a timer finishes is
up to whoever calls set_notifier(); tray.py shows a bubble from the orb and
chimes. With no notifier (the eval, the voice console) a finished timer is
just recorded, so the eval never beeps at him.
"""

import itertools
import threading
from datetime import datetime, timedelta

MAX_MINUTES = 24 * 60  # past a day it's a reminder, and add_task owns those

_lock = threading.Lock()
_timers = {}  # id -> {"id", "label", "ends_at", "minutes"}
_ids = itertools.count(1)
_notifier = None  # called as notifier(timer_dict) on the clock thread
_listeners = []  # called with no args whenever the set of timers changes
_thread = None


def set_notifier(fn):
    """Who gets told when a timer finishes. tray.py marshals onto Tk."""
    global _notifier
    _notifier = fn


def add_listener(fn):
    """Told (no args) whenever a timer starts, stops or finishes -- the
    countdown pill uses it to appear and disappear."""
    _listeners.append(fn)


def _changed():
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            pass  # a broken listener must never stop the clock


def _ensure_clock():
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_clock, daemon=True, name="denisse-timers")
        _thread.start()


def _clock():
    stop = threading.Event()
    while True:
        stop.wait(0.5)
        now = datetime.now()
        with _lock:
            due = [t for t in _timers.values() if t["ends_at"] <= now]
            for t in due:
                del _timers[t["id"]]
        for t in due:
            if _notifier is not None:
                try:
                    _notifier(dict(t))
                except Exception:
                    pass
        if due:
            _changed()


def _fmt_span(minutes: float) -> str:
    secs = round(minutes * 60)
    if secs < 60:
        return f"{secs} second{'s' if secs != 1 else ''}"
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(parts)


def _num(v):
    if v in (None, ""):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{v!r} isn't a number")


def active() -> list:
    """Running timers, soonest first, as plain dicts."""
    with _lock:
        return sorted((dict(t) for t in _timers.values()), key=lambda t: t["ends_at"])


def set_timer(minutes=None, seconds=None, label: str = "") -> str:
    """Start a countdown. Returns what actually happened, in plain words."""
    try:
        total = _num(minutes) + _num(seconds) / 60
    except ValueError as e:
        return f"Error: {e}. No timer was started."
    if total <= 0:
        # An 8B model will call this with no length for "start a timer".
        # Refusing with the reason makes it ask, rather than inventing one.
        return ("No timer started: no length was given. Ask Chuen how long "
                "the timer should be.")
    if total > MAX_MINUTES:
        return ("No timer started: that's longer than a day. Offer to add it "
                "as a task with a deadline instead.")
    label = (label or "").strip()
    if label.lower() in ("timer", "a timer", "the timer", "countdown"):
        label = ""  # the model's filler, not a name -- else "'timer' timer"
    with _lock:
        tid = next(_ids)
        ends = datetime.now() + timedelta(minutes=total)
        _timers[tid] = {"id": tid, "label": label, "ends_at": ends, "minutes": total}
    _ensure_clock()
    _changed()
    what = f"'{label}' timer" if label else "Timer"
    return f"{what} started: {_fmt_span(total)}, done at {ends:%H:%M:%S}."


def cancel_timer(label: str = "") -> str:
    """Stop a running timer -- the one whose label matches, or, with no
    label, the only one (or every one, if he says stop and there are a few:
    "stop the timer" with two running is not worth a question)."""
    label = (label or "").strip().lower()
    with _lock:
        if not _timers:
            return "There's no timer running, so nothing was stopped."
        if label and label not in ("timer", "the timer", "all", "everything"):
            hits = [t for t in _timers.values() if label in t["label"].lower()]
            if not hits:
                names = ", ".join(f"'{t['label']}'" if t["label"] else "an unnamed one"
                                  for t in _timers.values())
                return f"No timer called '{label}' is running (running: {names}). Nothing was stopped."
        else:
            hits = list(_timers.values())
        for t in hits:
            del _timers[t["id"]]
    _changed()
    if len(hits) == 1:
        t = hits[0]
        name = f"'{t['label']}' timer" if t["label"] else "Timer"
        left = max(0.0, (t["ends_at"] - datetime.now()).total_seconds() / 60)
        return f"{name} stopped with {_fmt_span(left)} left."
    return f"Stopped {len(hits)} timers."


def list_timers() -> str:
    timers = active()
    if not timers:
        return "No timers running."
    now = datetime.now()
    lines = []
    for t in timers:
        left = max(0.0, (t["ends_at"] - now).total_seconds() / 60)
        name = f"'{t['label']}'" if t["label"] else "Timer"
        lines.append(f"{name}: {_fmt_span(left)} left (done at {t['ends_at']:%H:%M:%S})")
    return "\n".join(lines)
