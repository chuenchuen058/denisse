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

import safe_json
import tasks

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Every path, pattern, offset and sentence lives in the JSON beside this
# file (Rule 0). Read at call time, so editing CONFIG changes behaviour.
with open(os.path.splitext(os.path.abspath(__file__))[0] + ".json", encoding="utf-8-sig") as _config_file:
    CONFIG = json.load(_config_file)

PENDING_FILE = os.path.join(BASE_DIR, *CONFIG["pending_file"].split("/"))

from settings import vault as _vault

VAULT = _vault()


def _pattern(name: str):
    return re.compile(CONFIG["patterns"][name])


def _parse_date(s: str):
    for fmt in CONFIG["date_formats"]:
        try:
            return dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _module_name(path: str) -> str:
    folder = os.path.basename(os.path.dirname(path))
    if folder.lower() == CONFIG["coursework_folder"]:
        folder = os.path.basename(os.path.dirname(os.path.dirname(path)))
    name = _pattern("code").sub("", folder)
    name = _pattern("module_noise").sub("", name)
    return " ".join(name.split()) or folder


def coursework_deadlines(vault: str = VAULT) -> list:
    """Every dated coursework deadline in his module notes, soonest first.

    Reads each module's Coursework.md: the frontmatter description first
    (that is where he keeps "due 24 Nov 2026"), then the body. Worked
    solutions and other files that merely mention the coursework are skipped.
    """
    due_re, code_re, weight_re = _pattern("due"), _pattern("code"), _pattern("weight")
    found = {}
    for path in glob.glob(os.path.join(vault, *CONFIG["coursework_glob"].split("/")), recursive=True):
        if os.path.basename(path).lower() != CONFIG["coursework_file"]:
            continue
        try:
            text = open(path, encoding="utf-8-sig", errors="replace").read()
        except OSError:
            continue
        desc = ""
        m = _pattern("description").search(text)
        if m:
            desc = m.group(1)
        hit = due_re.search(desc) or due_re.search(text)
        due = _parse_date(hit.group(1)) if hit else None
        if not due:
            continue
        code_m = code_re.search(desc) or code_re.search(path)
        code = code_m.group(1) if code_m else ""
        weight = weight_re.search(desc)
        module = _module_name(path)
        found[(module, due)] = {
            "module": module,
            "code": code,
            "due": due,
            "weight": CONFIG["weight_format"].format(weight=weight.group(1)) if weight else "",
            "brief_unread": CONFIG["brief_unread_marker"] in desc.lower(),
            "path": path,
        }
    return sorted(found.values(), key=lambda d: d["due"])


def _when(d: dt.date, today: dt.date) -> str:
    words = CONFIG["when"]
    days = (d - today).days
    if days == 0:
        return words["today"]
    if days == 1:
        return words["tomorrow"]
    if days < 0:
        return words["past"].format(days=-days)
    return words["future"].format(days=days)


def _short(t: dict, n: int | None = None) -> str:
    """tasks.describe with the task text cut at a word -- his msoc tasks run
    to 150 characters, which is a paragraph when read aloud."""
    n = CONFIG["short_task_chars"] if n is None else n
    text = t["text"]
    if len(text) > n:
        text = text[:n].rsplit(" ", 1)[0].rstrip(CONFIG["short_task_strip"]) + CONFIG["ellipsis"]
    return tasks.describe({**t, "text": text})


def _join(items: list, limit: int | None = None) -> str:
    cfg = CONFIG["join"]
    limit = cfg["limit"] if limit is None else limit
    shown = items[:limit]
    more = len(items) - len(shown)
    return cfg["separator"].join(shown) + (cfg["more"].format(more=more) if more > 0 else "")


def briefing(now: dt.datetime | None = None, horizon_days: int | None = None) -> str:
    """Today, from real data only: what's overdue, what's due soon, and the
    coursework coming up. Spoken-friendly sentences -- this is often read
    aloud. No news (he switched it off) and no weather unless a weather
    tool actually ran, which it doesn't here."""
    cfg = CONFIG["briefing"]
    horizon_days = cfg["horizon_days"] if horizon_days is None else horizon_days
    now = now or dt.datetime.now()
    today = now.date()
    parts = [cfg["clock"].format(now=now)]

    overdue, soon = tasks.due_within(cfg["soon_hours"])
    if overdue:
        parts.append(cfg["overdue"].format(items=_join([_short(t) for t in overdue])))
    if soon:
        parts.append(cfg["soon"].format(items=_join([_short(t) for t in soon])))

    upcoming = [d for d in coursework_deadlines()
                if 0 <= (d["due"] - today).days <= horizon_days]
    if upcoming:
        bits = [cfg["coursework_item"].format(module=d["module"], due=d["due"],
                                              when=_when(d["due"], today))
                for d in upcoming]
        parts.append(cfg["coursework"].format(items=_join(bits)))
        unread = [d["module"] for d in upcoming if d["brief_unread"]]
        if unread:
            parts.append(cfg["unread"].format(items=_join(unread)))

    if not (overdue or soon or upcoming):
        parts.append(cfg["nothing"].format(horizon_days=horizon_days))
    return " ".join(parts)


def study_plan(now: dt.datetime | None = None, horizon_days: int | None = None) -> tuple:
    """A dated plan for each upcoming coursework, and the steps as tasks.

    Up to three steps per piece of coursework, counted back from the
    deadline (offsets in planner.json): read the brief (only if his note
    says it's unread), a full draft, then finish and submit. Steps already
    on his task list are left out. Returns (text, steps)."""
    cfg = CONFIG["study_plan"]
    horizon_days = cfg["horizon_days"] if horizon_days is None else horizon_days
    day = dt.timedelta(days=1)
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
            plan.append((min(today + day * cfg["read_brief_after_days"],
                             due - day * cfg["read_brief_before_due_days"]),
                         cfg["read_brief"].format(module=mod)))
        if days > cfg["draft_min_days_left"]:
            plan.append((due - day * cfg["draft_before_due_days"], cfg["draft"].format(module=mod)))
        plan.append((max(today, due - day * cfg["submit_before_due_days"]),
                     cfg["submit"].format(module=mod)))
        head = cfg["head"].format(module=mod, due=due) + (
            cfg["head_weight"].format(weight=d["weight"]) if d["weight"] else "")
        lines.append(head)
        for when, text in plan:
            lines.append(cfg["line"].format(when=when, text=text[0].lower() + text[1:]))
            if text.lower() not in existing:
                steps.append({"text": text,
                              "due": cfg["step_due"].format(when=when, time=cfg["step_due_time"])})
    if not lines:
        return cfg["none"], []
    return cfg["header"] + "\n".join(lines) + cfg["footer"], steps


# -- offering the plan, and acting on "yes" ---------------------------------


def offer(steps: list, now: dt.datetime | None = None) -> str:
    """Hold the steps until he answers. Returns the question to append, or
    '' when there is nothing new to add."""
    if not steps:
        return ""
    now = now or dt.datetime.now()
    safe_json.write_json_atomic(PENDING_FILE, {"created": now.isoformat(timespec="seconds"), "steps": steps},
                                indent=2)
    return CONFIG["offer"].format(n=len(steps))


def pending(now: dt.datetime | None = None) -> list:
    now = now or dt.datetime.now()
    try:
        data = safe_json.read_json(PENDING_FILE, None)
        made = dt.datetime.fromisoformat(data["created"])
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if now - made > dt.timedelta(minutes=CONFIG["pending_minutes"]):
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
    return (CONFIG["accepted"].format(n=len(added))
            + "\n".join(CONFIG["accepted_line"].format(task=tasks.describe(t)) for t in added))
