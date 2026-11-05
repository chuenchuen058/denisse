"""
Denisse's task list: things Chuen has to do, with optional deadlines.

Deliberately NOT a calendar. "Email Sunway before Friday" is a task with a
due date, not a meeting -- putting every todo on a calendar makes it
unreadable, and deleting done items would erase the record that they
happened. Tasks live here; only things that need protected time become
calendar events (see calendar_ms.py), and the event id is stored on the
task so the two stay linked.

Done tasks are kept, not deleted -- `list_tasks()` hides them, so they're
out of the way, but "what did I get done this week" stays answerable.
"""

import calendar
import json
import os
import re
import uuid
from datetime import datetime, timedelta

import reminders_config as _cfg

# The file name, priorities, limits, word lists and wording are in
# reminders.json (Rule 0): "files", "priorities" and "tasks".
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = os.path.join(BASE_DIR, *re.split(r"[\\/]", _cfg.get("files.tasks")))

# Weekday and month names come from the calendar module, not a list here.
_WEEKDAYS = [d.lower() for d in calendar.day_name]
_MONTHS = [m.lower() for m in calendar.month_abbr if m]


def _words(key: str) -> str:
    """A regex alternation of the phrases listed at tasks.<key>, longest
    first so "no later than" wins over a shorter prefix."""
    words = sorted(_cfg.get("tasks." + key), key=len, reverse=True)
    return "|".join(re.escape(w) for w in words) or r"(?!)"


def _lead():
    # dateparser handles "tomorrow 5pm" and "in 2 hours" but returns None for
    # "before friday" and "next monday 9am" -- measured. Stripping the leading
    # preposition fixes the first; the weekday rule in parse_due the second.
    return re.compile(r"^\s*(?:" + _words("deadline_lead_words") + r")\s+", re.I)


def _meridiems() -> str:
    """am|pm -- the canonical names in time_parse.json "meridiems", so both
    parsers read the same words."""
    import time_parse

    return "|".join(re.escape(m) for m in time_parse.CONFIG["meridiems"])


def _end_of_day():
    """(hour, minute) a day with no time means -- time_parse.json's
    end_of_day, so both parsers agree."""
    import time_parse

    return time_parse.END_OF_DAY.hour, time_parse.END_OF_DAY.minute


def _next_weekday(name: str, hour: int, minute: int, now: datetime) -> datetime:
    """Next occurrence of a named weekday, strictly in the future."""
    target = _WEEKDAYS.index(name)
    ahead = (target - now.weekday()) % 7
    if ahead == 0:
        ahead = 7
    day = now + timedelta(days=ahead)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _local(now: datetime | None) -> datetime:
    """Naive London wall-clock time -- what the task store holds. Read from
    time_parse's clock so a pinned clock (tests, the eval) pins tasks too."""
    import time_parse

    if now is None:
        now = time_parse.now()
    if now.tzinfo is not None:
        now = now.astimezone(time_parse.LONDON).replace(tzinfo=None)
    return now


def parse_due(text: str, now: datetime | None = None) -> datetime | None:
    """Best-effort natural-language deadline. Returns None if unparseable --
    a task with an unreadable deadline is still worth keeping, so callers
    store the raw phrase either way rather than rejecting the task."""
    if not text or not text.strip():
        return None
    now = _local(now)
    # Deterministic parsing first: "thursday at 3" is 15:00 there, where
    # the weekday regex below read it as 03:00. An ambiguous phrase (the
    # weekday that is today) falls through to the old next-week reading;
    # tools.add_task asks Chuen before it ever gets here.
    import time_parse

    parsed = time_parse.parse(text, now)
    if parsed.when is not None:
        return parsed.when.astimezone(time_parse.LONDON).replace(tzinfo=None)
    cleaned = _lead().sub("", text.strip())

    # "next monday 9am" / "friday" / "this friday 5pm" -- dateparser returns
    # None for several of these, so weekday names are resolved directly.
    m = re.match(
        r"^(?:" + _words("weekday_qualifiers") + r")?\s*(" + "|".join(_WEEKDAYS) + r")"
        r"(?:\s+(?:(?:" + _words("clock_lead_words") + r")\s+)?(\d{1,2})(?::(\d{2}))?\s*("
        + _meridiems() + r")?)?\s*$",
        cleaned, re.I,
    )
    if m:
        hour, minute = _end_of_day()  # a bare "friday" means end of Friday
        if m.group(2):
            hour = int(m.group(2))
            minute = int(m.group(3) or 0)
            am, pm = _meridiems().split("|")
            ampm = (m.group(4) or "").lower()
            if ampm == pm and hour < 12:
                hour += 12
            elif ampm == am and hour == 12:
                hour = 0
        return _next_weekday(m.group(1).lower(), hour, minute, now)

    try:
        import dateparser

        when = dateparser.parse(
            cleaned,
            settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now},
        )
    except Exception:
        return None
    if when is None or _has_time().search(cleaned):
        return when
    # A day with no time is the end of that day, like a bare "friday" above.
    # dateparser keeps the clock instead: "tomorrow" said at 00:03 became a
    # task due tomorrow 00:03 (2026-10-31). "before 2 Nov" is the end of
    # the day before.
    hour, minute = _end_of_day()
    when = when.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if re.match(r"^\s*(?:" + _words("day_before_words") + r")\b", text, re.I):
        when -= timedelta(days=1)
    return when


def _has_time():
    """Anything that says when in the day, or a span measured from now: an
    am/pm hour, a clock time, a bare hour, '<clock_lead_word> <hour>', or
    one of tasks.time_words."""
    return re.compile(
        r"\d{1,2}\s*(?:" + _meridiems() + r")\b|\d{1,2}[:.h]\d{2}\b"
        r"|\b(?:" + _words("time_words") + r")\b"
        r"|^\s*\d{1,2}\s*$|\b(?:" + _words("clock_lead_words") + r")\s+\d{1,2}\b",
        re.IGNORECASE)


def due_from_text(text: str, now: datetime | None = None) -> datetime | None:
    """Pull a deadline out of the task text itself.

    People write the date inside the sentence -- "live online meeting on the
    6th of October 14:00 (Tue 06 Oct 14:00)" -- and the model then passes
    the whole thing as `text` with no `due` at all, so the task lands with
    no deadline and never triggers a reminder. Observed 2026-09-25 with two
    real tasks.

    Parentheses defeat dateparser's search outright (measured: the string
    above returns None, the same string without the bracketed part parses
    fine), so they're stripped first. The last future date in the sentence
    wins -- in "meeting on the 6th (Tue 06 Oct)" both refer to the same
    thing, and where they differ the later one is the deadline.
    """
    if not text:
        return None
    now = _local(now)
    cleaned = re.sub(r"\([^)]*\)", " ", text)
    try:
        from dateparser.search import search_dates

        found = search_dates(
            cleaned,
            settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now},
        )
    except Exception:
        return None
    if not found:
        return None
    for phrase, when in reversed(found):
        # A match with no digit and no month/weekday name is almost always
        # a false positive off an ordinary word ("may", "march", "sat").
        if not when or when <= now:
            continue
        if re.search(r"\d", phrase) or re.search(
            r"(?i)\b(" + "|".join(_WEEKDAYS + _MONTHS) + "|" + _words("inferred_date_words") + ")",
            phrase,
        ):
            return when
    return None


def _load() -> list:
    if not os.path.exists(TASKS_FILE):
        return []
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(tasks: list) -> None:
    os.makedirs(os.path.dirname(TASKS_FILE), exist_ok=True)
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)
    _mirror_to_vault()


def _mirror_to_vault() -> None:
    """Keep the Obsidian note in step with every change.

    Imported lazily and swallowed whole: obsidian_tasks imports this module
    back, and more importantly a vault problem (OneDrive not synced, file
    open in Obsidian and locked) must never be the reason a task fails to
    save. The JSON on disk is the source of truth; the note is a mirror.
    """
    try:
        import obsidian_tasks
        import sys

        obsidian_tasks.sync(sys.modules[__name__])
    except Exception:
        pass


def pull_vault_ticks() -> list:
    """Close anything Chuen ticked in the Obsidian note. Returns what closed."""
    try:
        import obsidian_tasks

        ids = obsidian_tasks.pull_ticked()
    except Exception:
        return []
    closed = []
    for task_id in ids:
        t = complete_task(task_id)
        if t is not None:
            closed.append(t)
    return closed


PRIORITIES = tuple(_cfg.priorities())


def _norm_priority(p) -> str:
    """One of the priorities in reminders.json; anything else (or nothing)
    is tasks.default_priority, so tasks from before priorities existed
    still read sensibly."""
    p = (p or "").strip().lower()
    return p if p in _cfg.priorities() else _cfg.get("tasks.default_priority")


def add_task(text: str, due: str = "", priority: str | None = None) -> dict:
    """Add a task. `due` is free text ("before friday", "tomorrow 5pm");
    the raw phrase is kept alongside the parsed datetime so nothing is lost
    when parsing fails."""
    when = parse_due(due)
    inferred = False
    if when is None:
        # No usable `due` -- the date is often sitting in the text instead.
        when = due_from_text(text)
        inferred = when is not None
    task = {
        "id": uuid.uuid4().hex[:_cfg.get("tasks.id_length")],
        "text": text.strip(),
        "due": when.isoformat(timespec="minutes") if when else None,
        "due_text": due.strip() or None,
        "created": datetime.now().isoformat(timespec="minutes"),
        "done_at": None,
        "due_inferred": inferred,  # read out of the text, not given as `due`
        "event_id": None,      # set when it also becomes a calendar event
        "reminded_on": None,   # date we last nagged, so login reminders don't repeat
        "priority": _norm_priority(priority),  # colours the reminder card
    }
    tasks = _load()
    tasks.append(task)
    _save(tasks)
    return task


def _content_words(text: str) -> set:
    """Words that identify a task. tasks.match.ignore_words carry no
    identifying weight: without them, "mark the sunway email done" matched
    "submit the EE problem set" on the word "the" alone -- i.e. it would
    have completed the wrong task. Caught by test_tasks.py."""
    match = _cfg.get("tasks.match")
    ignore = set(match["ignore_words"])
    return {
        w for w in re.findall(r"\w+", (text or "").lower())
        if len(w) >= match["min_word_length"] and w not in ignore
    }


def _match(tasks: list, needle: str) -> dict | None:
    """Find a task by id, or by words from its text.

    The model will say "mark the Sunway email done", not an 8-char id, so
    matching has to be forgiving -- but only among open tasks, and only on
    an unambiguous best match, so "done" never completes the wrong thing.
    """
    needle = (needle or "").strip().lower()
    if not needle:
        return None
    open_tasks = [t for t in tasks if not t["done_at"]]

    for t in open_tasks:
        if t["id"] == needle:
            return t

    words = _content_words(needle)
    if not words:
        return None
    scored = []
    for t in open_tasks:
        have = _content_words(t["text"])
        overlap = len(words & have)
        if overlap:
            scored.append((overlap, t))
    if not scored:
        return None
    scored.sort(key=lambda s: -s[0])
    # Ambiguous: two tasks match equally well. Refuse rather than guess.
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def list_tasks(include_done: bool = False) -> list:
    """Open tasks, soonest deadline first; undated ones last."""
    tasks = _load()
    if not include_done:
        tasks = [t for t in tasks if not t["done_at"]]
    return sorted(tasks, key=lambda t: (t["due"] is None, t["due"] or ""))


def complete_task(needle: str) -> dict | None:
    """Mark done. Kept, not deleted -- see the module docstring."""
    tasks = _load()
    t = _match(tasks, needle)
    if t is None:
        return None
    t["done_at"] = datetime.now().isoformat(timespec="minutes")
    _save(tasks)
    return t


def remove_task(needle: str) -> dict | None:
    """Delete outright -- for things added by mistake, not things done."""
    tasks = _load()
    t = _match(tasks, needle)
    if t is None:
        return None
    tasks = [x for x in tasks if x["id"] != t["id"]]
    _save(tasks)
    return t


def update_task(task_id: str, text: str | None = None, due: str | None = None,
                priority: str | None = None) -> dict | None:
    """Edit an open task in place, by exact id.

    For the reminder card's edit button, where the id is already known --
    so no fuzzy matching (that's _match's job, for the model's free-text
    requests). due=None leaves the deadline alone; due="" clears it; an
    unparseable due is kept as due_text, same as add_task. A changed
    deadline also clears reminded_on, so a task moved to tomorrow can
    still nag tomorrow.
    """
    tasks = _load()
    for t in tasks:
        if t["id"] != task_id or t["done_at"]:
            continue
        if text is not None and text.strip():
            t["text"] = text.strip()
        if due is not None:
            due = due.strip()
            when = parse_due(due) if due else None
            t["due"] = when.isoformat(timespec="minutes") if when else None
            t["due_text"] = due or None
            t["due_inferred"] = False
            t["reminded_on"] = None
        if priority is not None:
            t["priority"] = _norm_priority(priority)
        _save(tasks)
        return t
    return None


def due_within(hours: int | None = None) -> tuple:
    """(overdue, due_soon) among open tasks -- what a reminder should say.
    hours defaults to tasks.due_soon_hours."""
    if hours is None:
        hours = _cfg.get("tasks.due_soon_hours")
    now = datetime.now()
    horizon = now + timedelta(hours=hours)
    overdue, soon = [], []
    for t in list_tasks():
        if not t["due"]:
            continue
        when = datetime.fromisoformat(t["due"])
        if when < now:
            overdue.append(t)
        elif when <= horizon:
            soon.append(t)
    return overdue, soon


def mark_reminded(ids: list) -> None:
    """Record that we nagged today, so a reminder fires once per day and
    not on every tray restart."""
    today = datetime.now().date().isoformat()
    tasks = _load()
    for t in tasks:
        if t["id"] in ids:
            t["reminded_on"] = today
    _save(tasks)


def needs_reminder(hours: int | None = None) -> tuple:
    """Like due_within, but skips anything already flagged today."""
    today = datetime.now().date().isoformat()
    overdue, soon = due_within(hours)
    keep = lambda ts: [t for t in ts if t.get("reminded_on") != today]
    return keep(overdue), keep(soon)


def describe(t: dict) -> str:
    if not t["due"]:
        # Keep an unparseable deadline visible -- "as soon as possible"
        # is a real instruction even though no date comes out of it, and
        # silently dropping it makes the task look undated.
        if t.get("due_text"):
            return _cfg.say("tasks.describe.undated_note", text=t["text"], note=t["due_text"])
        return _cfg.say("tasks.describe.undated", text=t["text"])
    when = datetime.fromisoformat(t["due"])
    now = datetime.now()
    if when.date() == now.date():
        key = "today"
    elif when.date() == (now + timedelta(days=1)).date():
        key = "tomorrow"
    else:
        key = "later"
    return _cfg.say("tasks.describe." + key, text=t["text"], when=when)
