"""Denisse -- briefings and study plans built from his real data.

Asked on 2026-10-29 for "the briefing", the model checked the clock and
then invented the weather ("68 F, partly cloudy, high of 72 F") with no
weather tool called. Asked to "plan my study modules", it went looking
for a skill called plan_study_modules, told him to use list_skills, and
asked what subjects he studies -- while his deadlines sat in his own
module notes. Chuen: "i dont want her to reply me to satisfy me i want
her to actually do task".

So both are assembled here, deterministically, from things that exist:
coursework deadlines in the vault's Coursework.md notes and the open
task list. Nothing in this file asks a model to compose anything, so
nothing in it can be invented. What it doesn't know (lecture timetable,
exam dates, his tracker artifact) it says it doesn't know.

A plan is offered, not imposed: the steps are held in
memory/pending_plan.json and only become tasks when he says yes.
"""
import datetime as dt
import glob
import json
import os
import re

import tasks

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_FILE = os.path.join(BASE_DIR, "memory", "pending_plan.json")
PENDING_MINUTES = 30  # a "yes" an hour later is not about this plan

try:
    from vault_search import VAULT
except Exception:  # pragma: no cover -- vault_search always has it today
    VAULT = r"C:\Users\Xichu\OneDrive - King's College London\Denisse"

_DUE = re.compile(
    r"\bdue\s+(?:\*\*)?(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?"
    r"(\d{1,2}\s+[A-Za-z]{3,9}\s+20\d\d)", re.IGNORECASE)
_CODE = re.compile(r"\b(\d[A-Z]{4}\d{3})\b")
_WEIGHT = re.compile(r"\b(\d{1,3})%")


def _parse_date(s: str):
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _module_name(path: str) -> str:
    folder = os.path.basename(os.path.dirname(path))
    if folder.lower() == "coursework":
        folder = os.path.basename(os.path.dirname(os.path.dirname(path)))
    name = _CODE.sub("", folder)
    name = re.sub(r"\(.*?\)", "", name)
    return " ".join(name.split()) or folder


def coursework_deadlines(vault: str = VAULT) -> list:
    """Every dated coursework deadline in his module notes, soonest first.

    Reads each module's Coursework.md: the frontmatter description first
    (that is where he keeps "due 24 Nov 2026"), then the body. Worked
    solutions and other files that merely mention the coursework are skipped.
    """
    found = {}
    for path in glob.glob(os.path.join(vault, "SEM * Modules", "**", "Coursework*.md"),
                          recursive=True):
        if os.path.basename(path).lower() != "coursework.md":
            continue
        try:
            text = open(path, encoding="utf-8-sig", errors="replace").read()
        except OSError:
            continue
        desc = ""
        m = re.search(r"(?m)^description:\s*\"?(.+?)\"?\s*$", text)
        if m:
            desc = m.group(1)
        hit = _DUE.search(desc) or _DUE.search(text)
        due = _parse_date(hit.group(1)) if hit else None
        if not due:
            continue
        code_m = _CODE.search(desc) or _CODE.search(path)
        code = code_m.group(1) if code_m else ""
        weight = _WEIGHT.search(desc)
        module = _module_name(path)
        found[(module, due)] = {
            "module": module,
            "code": code,
            "due": due,
            "weight": f"{weight.group(1)}%" if weight else "",
            "brief_unread": "brief not yet read" in desc.lower(),
            "path": path,
        }
    return sorted(found.values(), key=lambda d: d["due"])


def _when(d: dt.date, today: dt.date) -> str:
    days = (d - today).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days < 0:
        return f"{-days} days ago"
    return f"in {days} days"


def _short(t: dict, n: int = 70) -> str:
    """tasks.describe with the task text cut at a word -- his msoc tasks run
    to 150 characters, which is a paragraph when read aloud."""
    text = t["text"]
    if len(text) > n:
        text = text[:n].rsplit(" ", 1)[0].rstrip(" ,;:-(") + "..."
    return tasks.describe({**t, "text": text})


def _join(items: list, limit: int = 4) -> str:
    shown = items[:limit]
    more = len(items) - len(shown)
    return "; ".join(shown) + (f"; and {more} more" if more > 0 else "")


def briefing(now: dt.datetime | None = None, horizon_days: int = 45) -> str:
    """Today, from real data only: what's overdue, what's due in the next
    two days, and the coursework coming up. Spoken-friendly sentences --
    this is often read aloud. No news (he switched it off) and no weather
    unless a weather tool actually ran, which it doesn't here."""
    now = now or dt.datetime.now()
    today = now.date()
    parts = [f"It's {now:%A %d %B}, {now:%H:%M}."]

    overdue, soon = tasks.due_within(48)
    if overdue:
        parts.append(f"Overdue: {_join([_short(t) for t in overdue])}.")
    if soon:
        parts.append(f"Due in the next two days: {_join([_short(t) for t in soon])}.")

    upcoming = [d for d in coursework_deadlines()
                if 0 <= (d["due"] - today).days <= horizon_days]
    if upcoming:
        bits = [f"{d['module']} {d['due']:%a %d %b} ({_when(d['due'], today)})"
                for d in upcoming]
        parts.append(f"Coursework due: {_join(bits)}.")
        unread = [d["module"] for d in upcoming if d["brief_unread"]]
        if unread:
            parts.append(f"Brief not read yet for {_join(unread)}.")

    if not (overdue or soon or upcoming):
        parts.append(f"Nothing overdue, and no coursework due in the next "
                     f"{horizon_days} days.")
    return " ".join(parts)


def study_plan(now: dt.datetime | None = None, horizon_days: int = 120) -> tuple:
    """A dated plan for each upcoming coursework, and the steps as tasks.

    Three steps per piece of coursework, counted back from the deadline:
    read the brief (only if his note says it's unread), have a full draft
    a week out, finish and submit the day before. Steps already on his task
    list are left out. Returns (text, steps)."""
    now = now or dt.datetime.now()
    today = now.date()
    existing = {t["text"].lower() for t in tasks.list_tasks()}
    lines, steps = [], []
    for d in coursework_deadlines():
        days = (d["due"] - today).days
        if days < 0 or days > horizon_days:
            continue
        mod, due = d["module"], d["due"]
        plan = []
        if d["brief_unread"]:
            plan.append((min(today + dt.timedelta(days=2), due - dt.timedelta(days=1)),
                         f"Read the {mod} coursework brief"))
        if days > 9:
            plan.append((due - dt.timedelta(days=7), f"Full draft of the {mod} coursework"))
        plan.append((max(today, due - dt.timedelta(days=1)),
                     f"Finish and submit the {mod} coursework"))
        head = f"{mod} -- due {due:%a %d %b}" + (f", {d['weight']}" if d["weight"] else "")
        lines.append(head)
        for when, text in plan:
            lines.append(f"  - {when:%a %d %b}: {text[0].lower() + text[1:]}")
            if text.lower() not in existing:
                steps.append({"text": text, "due": f"{when:%d %b %Y} 18:00"})
    if not lines:
        return ("I couldn't find any dated coursework in your module notes for "
                "the next few months, so there's nothing to plan from yet."), []
    text = ("Here's a plan from the coursework deadlines in your module notes:\n\n"
            + "\n".join(lines)
            + "\n\nThis covers coursework only -- I don't have your lecture "
              "timetable or exam dates.")
    return text, steps


# -- offering the plan, and acting on "yes" ---------------------------------

OFFER = "Want me to add {n} of these to your tasks?"


def offer(steps: list, now: dt.datetime | None = None) -> str:
    """Hold the steps until he answers. Returns the question to append, or
    '' when there is nothing new to add."""
    if not steps:
        return ""
    now = now or dt.datetime.now()
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump({"created": now.isoformat(timespec="seconds"), "steps": steps}, f, indent=2)
    return OFFER.format(n=len(steps))


def pending(now: dt.datetime | None = None) -> list:
    now = now or dt.datetime.now()
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            data = json.load(f)
        made = dt.datetime.fromisoformat(data["created"])
    except (OSError, ValueError, KeyError):
        return []
    if now - made > dt.timedelta(minutes=PENDING_MINUTES):
        return []
    return data.get("steps") or []


def clear_pending() -> None:
    try:
        os.remove(PENDING_FILE)
    except OSError:
        pass


def accept(now: dt.datetime | None = None) -> str:
    """He said yes: add the held steps as real tasks and say exactly what
    was added -- read back from the task store, not from the plan."""
    steps = pending(now)
    if not steps:
        return ""
    added = [tasks.add_task(s["text"], due=s["due"]) for s in steps]
    clear_pending()
    return (f"Added {len(added)} tasks:\n"
            + "\n".join(f"- {tasks.describe(t)}" for t in added))
