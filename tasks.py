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

import json
import os
import re
import uuid
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = os.path.join(BASE_DIR, "memory", "tasks.json")

# dateparser handles "tomorrow 5pm" and "in 2 hours" but returns None for
# "before friday" and "next monday 9am" -- measured. Stripping the leading
# preposition fixes the first; the second is handled by _WEEKDAY below.
_LEAD = re.compile(
    r"^\s*(?:by|before|due|on|at|until|till|no later than|nlt)\s+", re.I
)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]


def _next_weekday(name: str, hour: int, minute: int, now: datetime) -> datetime:
    """Next occurrence of a named weekday, strictly in the future."""
    target = _WEEKDAYS.index(name)
    ahead = (target - now.weekday()) % 7
    if ahead == 0:
        ahead = 7
    day = now + timedelta(days=ahead)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_due(text: str, now: datetime | None = None) -> datetime | None:
    """Best-effort natural-language deadline. Returns None if unparseable --
    a task with an unreadable deadline is still worth keeping, so callers
    store the raw phrase either way rather than rejecting the task."""
    if not text or not text.strip():
        return None
    now = now or datetime.now()
    cleaned = _LEAD.sub("", text.strip())

    # "next monday 9am" / "friday" / "this friday 5pm" -- dateparser returns
    # None for several of these, so weekday names are resolved directly.
    m = re.match(
        r"^(?:this|next|coming)?\s*(" + "|".join(_WEEKDAYS) + r")"
        r"(?:\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?\s*$",
        cleaned, re.I,
    )
    if m:
        hour, minute = 23, 59  # a bare "friday" means end of Friday
        if m.group(2):
            hour = int(m.group(2))
            minute = int(m.group(3) or 0)
            ampm = (m.group(4) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
        return _next_weekday(m.group(1).lower(), hour, minute, now)

    try:
        import dateparser

        return dateparser.parse(
            cleaned,
            settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now},
        )
    except Exception:
        return None


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
    now = now or datetime.now()
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
            r"(?i)\b(" + "|".join(_WEEKDAYS) + r"|jan|feb|mar|apr|may|jun|"
            r"jul|aug|sep|oct|nov|dec|today|tomorrow|tonight)", phrase
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


PRIORITIES = ("high", "medium", "low")


def _norm_priority(p) -> str:
    """high / medium / low; anything else (or nothing) is medium, so
    tasks from before priorities existed still read sensibly."""
    p = (p or "").strip().lower()
    return p if p in PRIORITIES else "medium"


def add_task(text: str, due: str = "", priority: str = "medium") -> dict:
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
        "id": uuid.uuid4().hex[:8],
        "text": text.strip(),
        "due": when.isoformat(timespec="minutes") if when else None,
        "due_text": due.strip() or None,
        "created": datetime.now().isoformat(timespec="minutes"),
        "done_at": None,
        "due_inferred": inferred,  # read out of the text, not given as `due`
        "event_id": None,      # set when it also becomes a calendar event
        "reminded_on": None,   # date we last nagged, so login reminders don't repeat
        "priority": _norm_priority(priority),  # high / medium / low -- colours the reminder card
    }
    tasks = _load()
    tasks.append(task)
    _save(tasks)
    return task


# Words that carry no identifying weight. Without this, "mark the sunway
# email done" matched "submit the EE problem set" on the word "the" alone --
# i.e. it would have completed the wrong task. Caught by test_tasks.py.
_STOPWORDS = {
    "the", "and", "for", "about", "with", "that", "this", "from", "into",
    "task", "todo", "item", "done", "mark", "off", "please", "can", "you",
    "was", "are", "get", "got", "not", "but", "all", "any", "one", "out",
    "new", "now", "its", "it's", "have", "has", "had", "need", "needs",
}


def _content_words(text: str) -> set:
    return {
        w for w in re.findall(r"\w+", (text or "").lower())
        if len(w) > 2 and w not in _STOPWORDS
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


def due_within(hours: int = 48) -> tuple:
    """(overdue, due_soon) among open tasks -- what a reminder should say."""
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


def needs_reminder(hours: int = 48) -> tuple:
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
            return f"{t['text']} ({t['due_text']})"
        return t["text"]
    when = datetime.fromisoformat(t["due"])
    now = datetime.now()
    if when.date() == now.date():
        return f"{t['text']} (today {when:%H:%M})"
    if when.date() == (now + timedelta(days=1)).date():
        return f"{t['text']} (tomorrow {when:%H:%M})"
    return f"{t['text']} ({when:%a %d %b %H:%M})"
