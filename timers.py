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

import reminders_config as _cfg

# Lengths, limits and every sentence below are in reminders.json under
# "timers" (Rule 0). Past max_minutes (a day) it's a reminder, and add_task
# owns those.
MAX_MINUTES = _cfg.get("timers.max_minutes")


def _say(key, **fields):
    return _cfg.say("timers.messages." + key, **fields)

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
        stop.wait(_cfg.get("timers.clock_poll_seconds"))
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


def _unit(n: int, unit: str) -> str:
    one, many = _cfg.get("timers.units." + unit)
    return f"{n} {one if n == 1 else many}"


def _fmt_span(minutes: float) -> str:
    secs = round(minutes * 60)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if not h and not m:
        return _unit(s, "second")
    return " ".join(_unit(n, u) for n, u in ((h, "hour"), (m, "minute"), (s, "second")) if n)


def _num(v):
    if v in (None, ""):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(_say("not_a_number", value=v))


def _num_or_none(v):
    try:
        return _num(v) or None
    except ValueError:
        return v  # not a number -- let set_timer report it


def active() -> list:
    """Running timers, soonest first, as plain dicts."""
    with _lock:
        return sorted((dict(t) for t in _timers.values()), key=lambda t: t["ends_at"])


def set_timer(minutes=None, seconds=None, label: str = "", duration: str = "") -> str:
    """Start a countdown. Returns what actually happened, in plain words.

    `duration` is his own words ("20 mins", "an hour and a half") and is
    turned into a length by time_parse, so the model needn't do the sums.
    Used only when no number was given; a phrase that isn't a length is
    refused rather than guessed at."""
    if duration and str(duration).strip() and _num_or_none(minutes) is None \
            and _num_or_none(seconds) is None:
        import time_parse

        span = time_parse.parse_duration(str(duration))
        if span is None:
            return _say("unreadable_duration", duration=duration)
        minutes, seconds = span.total_seconds() / 60, None
    try:
        total = _num(minutes) + _num(seconds) / 60
    except ValueError as e:
        return _say("bad_number", error=e)
    if total <= 0:
        # An 8B model will call this with no length for "start a timer".
        # Refusing with the reason makes it ask, rather than inventing one.
        return _say("no_length")
    if total > _cfg.get("timers.max_minutes"):
        return _say("too_long")
    label = (label or "").strip()
    if label.lower() in _cfg.get("timers.filler_labels"):
        label = ""  # the model's filler, not a name -- else "'timer' timer"
    with _lock:
        tid = next(_ids)
        ends = datetime.now() + timedelta(minutes=total)
        _timers[tid] = {"id": tid, "label": label, "ends_at": ends, "minutes": total}
    _ensure_clock()
    _changed()
    what = _say("named", label=label) if label else _say("unnamed")
    return _say("started", what=what, span=_fmt_span(total), ends=ends)


def cancel_timer(label: str = "") -> str:
    """Stop a running timer -- the one whose label matches, or, with no
    label, the only one (or every one, if he says stop and there are a few:
    "stop the timer" with two running is not worth a question)."""
    label = (label or "").strip().lower()
    with _lock:
        if not _timers:
            return _say("none_running")
        if label and label not in _cfg.get("timers.stop_all_labels"):
            hits = [t for t in _timers.values() if label in t["label"].lower()]
            if not hits:
                names = ", ".join(_say("running_name", label=t["label"]) if t["label"]
                                  else _say("unnamed_one") for t in _timers.values())
                return _say("no_match", label=label, names=names)
        else:
            hits = list(_timers.values())
        for t in hits:
            del _timers[t["id"]]
    _changed()
    if len(hits) == 1:
        t = hits[0]
        name = _say("named", label=t["label"]) if t["label"] else _say("unnamed")
        left = max(0.0, (t["ends_at"] - datetime.now()).total_seconds() / 60)
        return _say("stopped_one", name=name, span=_fmt_span(left))
    return _say("stopped_many", count=len(hits))


def list_timers() -> str:
    timers = active()
    if not timers:
        return _say("list_none")
    now = datetime.now()
    lines = []
    for t in timers:
        left = max(0.0, (t["ends_at"] - now).total_seconds() / 60)
        name = _say("list_named", label=t["label"]) if t["label"] else _say("list_unnamed")
        lines.append(_say("list_line", name=name, span=_fmt_span(left), ends=t["ends_at"]))
    return "\n".join(lines)
