"""Denisse -- a study brief for one module, as a job she finishes and checks.

Chuen chose this on 2026-10-30 as the first multi-step job: "prep me for
signals and systems" should end with a real file in his vault, not a reply
that sounds like one. The 28 Oct roadmap entry asked for exactly this shape
-- dependent steps, checked outputs, failure recovery, persistent progress.

    1. deadline   the module's dated coursework (planner.coursework_deadlines)
    2. coursework what's left to do: unticked items in its Progress / Plan
                  list, with marks and "revise from" when it has a question table
    3. weeks      which week notes are written up, and which are only files
    4. tasks      what's already on his task list for this module
    5. write      a dated brief into Denisse/Drafts/ in the vault
    6. verify     read the file back: due date, every unticked item, every
                  date inside the window, every link resolving
    7. offer      the dated steps, held until he says yes (planner.offer)

Like planner.py, no model composes anything here: every line comes from his
notes or his task list, so nothing in it can be invented. Progress is saved
after each step in memory/jobs/; a run that stopped part-way picks up from
the step that didn't finish, and a step that fails twice is reported as
failed -- never as done.
"""
import datetime as dt
import glob
import hashlib
import json
import os
import re

import planner
import tasks

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "memory", "jobs")
VAULT = planner.VAULT
DRAFTS = os.path.join(VAULT, "Denisse", "Drafts")
RESUME_MINUTES = 30  # an unfinished job older than this starts again

STEPS = ("deadline", "coursework", "weeks", "tasks", "write", "verify", "offer")
# Her question when no module was named; brain.py routes the answer back here.
WHICH_MODULE = "Which module is the study brief for?"


# -- which module he means -------------------------------------------------

_ALIASES = {
    "Signals and Systems": {"signals", "signal", "ss", "s&s", "sns", "5ccyb020"},
    "Electromagnetism": {"electromagnetism", "electromag", "em", "5ccyb010"},
    "Computational Methods": {"methods", "cm", "comp methods", "5ccyb070"},
    "Computational Applied Biomathematics": {"biomathematics", "biomaths", "biomath",
                                             "applied", "cab", "5ccyb080"},
}


def match_module(text: str) -> str | None:
    """The module named in what he said, or None. Matched on words, so
    "em" in "them" or "system" in "operating system" doesn't count."""
    low = " " + re.sub(r"[^a-z0-9&]+", " ", (text or "").lower()) + " "
    hits = [name for name, keys in _ALIASES.items()
            if any(f" {k} " in low for k in keys)]
    return hits[0] if len(hits) == 1 else None


# -- reading his notes -----------------------------------------------------

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)
_BOX = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$", re.MULTILINE)
_QROW = re.compile(r"^\|\s*\*\*(\d+)\*\*\s*\|(.+?)\|\s*(\d+)\s*\|(.+?)\|\s*$", re.MULTILINE)
_WK = re.compile(r"\bW(?:ee)?k\s*(\d{1,2})\b", re.IGNORECASE)


def _clip(text: str, n: int = 40) -> str:
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(" ,;(") + "..."


def _weeks_phrase(nums: list) -> str:
    """[2, 4, 5] -> 'Weeks 2, 4 and 5'; spoken, so no semicolons."""
    nums = [str(n) for n in nums]
    if len(nums) == 1:
        return f"Week {nums[0]}"
    return f"Weeks {', '.join(nums[:-1])} and {nums[-1]}"


def _lower_first(text: str) -> str:
    """'Read the brief' -> 'read the brief', but 'Q1 sketches' stays."""
    return text[0].lower() + text[1:] if len(text) > 1 and text[1].islower() else text


def _read(path: str) -> str:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def _front(text: str) -> dict:
    m = _FRONT.match(text or "")
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip().strip('"')
    return out


def _section(text: str, heading: str) -> str:
    m = re.search(rf"(?m)^##\s+{heading}\b.*$", text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"(?m)^##\s", rest)
    return rest[:nxt.start()] if nxt else rest


def read_coursework(path: str) -> dict:
    """Unticked work items, with marks and where to revise when the note has
    a question table (Signals does; the others are scaffolded from the
    deadline and have a Plan list instead)."""
    text = _read(path)
    questions = {}
    # "\|Ψ\|" inside a cell is an escaped pipe, not a column break.
    for q, topic, marks, revise in _QROW.findall(text.replace("\\|", "\x00")):
        topic = re.sub(r"\s+", " ", topic.replace("\x00", "|")).strip()
        revise = revise.replace("\x00", "|")
        questions[q] = {"topic": _clip(topic.split(" — ")[0].split(":")[0].strip()), "marks": int(marks),
                        "revise": " ".join(revise.split()),
                        "weeks": sorted({int(w) for w in _WK.findall(revise)})}
    box_src = _section(text, "Progress") or _section(text, "Plan")
    items = []
    for mark, label in _BOX.findall(box_src):
        q = re.match(r"Q(\d+)\b", label)
        item = {"text": label, "done": mark.lower() == "x"}
        if q and q.group(1) in questions:
            item.update(question=q.group(1), **questions[q.group(1)])
        items.append(item)
    feeds = sorted({int(w) for w in re.findall(r"\[\[[^]|]*Week (\d+)", _section(text, "Which week notes feed it"))})
    return {"items": items, "feeds": feeds, "has_questions": bool(questions)}


def read_weeks(module_dir: str) -> list:
    """Every week note: number, topic, and whether it is written up.
    `status: scaffolded` or `files-only` means the lecture isn't."""
    weeks = []
    for path in glob.glob(os.path.join(module_dir, "Week *", "* Week *.md")):
        fm = _front(_read(path))
        try:
            n = int(fm.get("week") or re.search(r"Week (\d+)", path).group(1))
        except (AttributeError, ValueError):
            continue
        status = fm.get("status", "")
        weeks.append({"week": n, "topic": fm.get("topic", ""),
                      "note": os.path.splitext(os.path.basename(path))[0],
                      "written": status not in ("scaffolded", "files-only"),
                      "status": status or "written"})
    return sorted(weeks, key=lambda w: w["week"])


def module_tasks(module: str, code: str) -> list:
    keys = {module.lower(), code.lower()} - {""}
    return [t for t in tasks.list_tasks()
            if any(k in t["text"].lower() for k in keys)]


# -- the schedule ----------------------------------------------------------

def schedule(work: list, today: dt.date, due: dt.date) -> list:
    """Spread the work from today to two days before the deadline, weighted
    by marks (unweighted items count as 10), and keep the day before for
    assembling and submitting. Returns [(date, text)] in order."""
    last = max(today, due - dt.timedelta(days=2))
    span = (last - today).days
    total = sum(w.get("weight", 10) for w in work) or 1
    out, done = [], 0
    for w in work:
        done += w.get("weight", 10)
        out.append((today + dt.timedelta(days=round(span * done / total)), w["text"]))
    out.append((max(today, due - dt.timedelta(days=1)), "Assemble, check and submit on KEATS"))
    return out


# -- the job ---------------------------------------------------------------

def _job_file(module: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", module.lower()).strip("-")
    return os.path.join(JOBS_DIR, f"study_brief-{slug}.json")


def _save(job: dict) -> None:
    os.makedirs(JOBS_DIR, exist_ok=True)
    tmp = job["file"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2, default=str)
    os.replace(tmp, job["file"])


def _load(module: str, now: dt.datetime) -> dict:
    path = _job_file(module)
    try:
        with open(path, encoding="utf-8") as f:
            job = json.load(f)
        started = dt.datetime.fromisoformat(job["started"])
        if job.get("status") == "running" and now - started <= dt.timedelta(minutes=RESUME_MINUTES):
            job["resumed"] = True
            return job
    except (OSError, ValueError, KeyError):
        pass
    return {"file": path, "module": module, "started": now.isoformat(timespec="seconds"),
            "status": "running", "done": {}, "errors": {}, "resumed": False}


def _brief_text(job: dict, today: dt.date) -> str:
    d, cw, weeks = job["done"]["deadline"], job["done"]["coursework"], job["done"]["weeks"]
    due = dt.date.fromisoformat(d["due"])
    mod = job["module"]
    cw_link = os.path.relpath(d["path"], VAULT).replace(os.sep, "/")
    todo = [i for i in cw["items"] if not i["done"]]
    by_week = {w["week"]: w for w in weeks}
    lines = [
        "---",
        f"name: {today:%Y-%m-%d} {mod} study brief",
        f'description: "Study brief for {mod}: coursework due {due:%d %b %Y}, '
        f'{len(todo)} of {len(cw["items"])} items left. Built by Denisse from the module notes."',
        "tags: [denisse, draft, study-brief]",
        f"updated: {today:%Y-%m-%d}",
        "---",
        "",
        f"# {mod} — study brief",
        "",
        f"> [!important] Due **{due:%a %d %b %Y}** ({planner._when(due, today)})"
        + (f" · {d['weight']}" if d["weight"] else ""),
        f"> From [[{cw_link}|the coursework note]]. Built {today:%d %b} from your notes only.",
        "",
        "## Where you are",
        "",
        f"- **{len(cw['items']) - len(todo)} of {len(cw['items'])}** coursework items ticked.",
    ]
    if d["brief_unread"]:
        lines.append("- **The brief isn't read yet** — the plan below is guessed from the deadline until it is.")
    gaps = [w for w in weeks if not w["written"]]
    if gaps:
        lines.append("- Not written up yet: " + ", ".join(
            f"[[{w['note']}|Week {w['week']}]] ({w['status']})" for w in gaps) + ".")
    if job["done"]["tasks"]:
        lines.append(f"- On your task list already: {len(job['done']['tasks'])} — "
                     + "; ".join(t["text"][:60] for t in job["done"]["tasks"][:3]) + ".")
    lines += ["", "## What's left", ""]
    if cw["has_questions"]:
        lines += ["| Item | Marks | Revise from |", "|---|---|---|"]
        for i in todo:
            lines.append(f"| {i['text']} | {i.get('marks', '')} | {i.get('revise', '')} |")
    else:
        lines += [f"- [ ] {i['text']}" for i in todo]
    needed = {w for i in todo for w in i.get("weeks", [])}
    if not cw["has_questions"]:
        needed |= set(cw["feeds"])  # no table: every week the note says feeds it
    missing = sorted(w for w in needed if w in by_week and not by_week[w]["written"])
    lines += ["", "## Plan", ""]
    planned = {int(m) for _, t in job["done"]["plan"] for m in re.findall(r"^Write up Week (\d+)", t)}
    if missing:
        listed = [w for w in missing if w not in planned]
        note = (f"{_weeks_phrase(missing)} {'isn' if len(missing) == 1 else 'aren'}'t written up, "
                "and questions below need them -- do the write-up before the question.")
        if listed:
            note += f" {_weeks_phrase(listed)} {'is' if len(listed) == 1 else 'are'} already on your task list."
        lines += [note, ""]
    for when, text in job["done"]["plan"]:
        lines.append(f"- by **{dt.date.fromisoformat(when):%a %d %b}** — {text}")
    untaught = [i["text"] for i in todo if i.get("question") and not i.get("weeks")]
    lines += ["", "## What I don't know", ""]
    if untaught:
        lines.append("- Not linked to a taught week yet: " + "; ".join(t[:50] for t in untaught) + ".")
    lines.append("- Your lecture timetable and exam dates — the plan covers this coursework only.")
    return "\n".join(lines) + "\n"


_LINK = re.compile(r"\[\[([^]|#]+)(?:#[^]|]*)?(?:\|[^]]*)?\]\]")


def verify(path: str, job: dict, today: dt.date) -> list:
    """Problems with the written brief, read back from disk. Empty = good."""
    try:
        text = _read(path)
    except OSError as exc:
        return [f"can't read it back: {exc}"]
    problems = []
    due = dt.date.fromisoformat(job["done"]["deadline"]["due"])
    if f"{due:%a %d %b %Y}" not in text:
        problems.append("the due date isn't in it")
    for i in job["done"]["coursework"]["items"]:
        if not i["done"] and i["text"] not in text:
            problems.append(f"missing item: {i['text'][:40]}")
    for when, _ in job["done"]["plan"]:
        if not today <= dt.date.fromisoformat(when) <= due:
            problems.append(f"plan date {when} is outside today..deadline")
    names = {os.path.splitext(os.path.basename(p))[0].lower()
             for p in glob.glob(os.path.join(VAULT, "**", "*.md"), recursive=True)}
    for target in _LINK.findall(text):
        target = target.strip()
        if "/" in target:
            if not os.path.exists(os.path.join(VAULT, target)):
                problems.append(f"broken link: {target}")
        elif target.lower() not in names:
            problems.append(f"broken link: {target}")
    return problems


def _hash(path: str) -> str:
    try:
        return hashlib.sha256(_read(path).encode("utf-8")).hexdigest()
    except OSError:
        return ""


# What she last wrote to each brief, across runs -- so a brief he has edited
# since is recognised as his and never overwritten.
def _written_file() -> str:
    return os.path.join(JOBS_DIR, "study_brief-written.json")


def _written() -> dict:
    try:
        with open(_written_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _remember_written(path: str) -> None:
    seen = _written()
    seen[path] = _hash(path)
    os.makedirs(JOBS_DIR, exist_ok=True)
    with open(_written_file(), "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2)


def run(text: str, now: dt.datetime | None = None) -> str:
    """Do the whole job for the module named in `text`. Returns what she says."""
    now = now or dt.datetime.now()
    today = now.date()
    module = match_module(text)
    if not module:
        return (WHICH_MODULE + " I can do one for Signals and Systems, "
                "Electromagnetism, Computational Methods or Computational Applied Biomathematics.")
    job = _load(module, now)

    def step(name, fn):
        if name in job["done"]:
            return job["done"][name]  # finished in an earlier, interrupted run
        for attempt in (1, 2):
            try:
                job["done"][name] = fn()
                job["errors"].pop(name, None)
                _save(job)
                return job["done"][name]
            except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
                job["errors"][name] = f"{type(exc).__name__}: {exc}"
        job["status"] = "failed"
        _save(job)
        raise RuntimeError(f"{name} step failed ({job['errors'][name].split(': ', 1)[-1]})")

    try:
        def find_deadline():
            hits = [d for d in planner.coursework_deadlines(VAULT)
                    if d["module"] == module and d["due"] >= today]
            if not hits:
                raise LookupError(f"no upcoming dated coursework for {module} in the module notes")
            d = hits[0]
            return {**d, "due": d["due"].isoformat()}

        d = step("deadline", find_deadline)
        due = dt.date.fromisoformat(d["due"])
        cw = step("coursework", lambda: read_coursework(d["path"]))
        module_dir = os.path.dirname(d["path"])
        if os.path.basename(module_dir).lower() == "coursework":
            module_dir = os.path.dirname(module_dir)
        weeks = step("weeks", lambda: read_weeks(module_dir))
        step("tasks", lambda: [{"text": t["text"], "due": str(t.get("due") or "")}
                               for t in module_tasks(module, d.get("code", ""))])

        def make_plan():
            by_week = {w["week"]: w for w in weeks}
            work, written_up = [], set()
            listed = " | ".join(t["text"].lower() for t in job["done"]["tasks"])

            def write_up(w):
                if re.search(rf"write[ -]?up[^|]*\bweek {w}\b", listed):
                    written_up.add(w)  # already on his task list -- don't add it twice
                if w in by_week and not by_week[w]["written"] and w not in written_up:
                    written_up.add(w)
                    work.append({"text": f"Write up Week {w} ({by_week[w]['topic'] or 'lecture'})",
                                 "weight": 6})

            if d["brief_unread"]:
                work.append({"text": f"Read the {module} coursework brief", "weight": 5})
            if not cw["has_questions"]:
                # No question table to say which week each part needs: write
                # up every gap among the weeks the note says feed it, first.
                for w in cw["feeds"]:
                    write_up(w)
            for i in cw["items"]:
                if i["done"] or re.match(r"(?i)assemble|submit|read the brief", i["text"]):
                    continue
                for w in i.get("weeks", []):
                    write_up(w)
                label = i["text"] + (f" ({i['topic']}, {i['marks']} marks)" if i.get("marks") else "")
                work.append({"text": label, "weight": i.get("marks", 10)})
            return [(when.isoformat(), t) for when, t in schedule(work, today, due)]

        job["done"].setdefault("plan", None)
        if job["done"]["plan"] is None:
            job["done"]["plan"] = make_plan()
            _save(job)

        def write():
            os.makedirs(DRAFTS, exist_ok=True)
            path = os.path.join(DRAFTS, f"{today:%Y-%m-%d} {module} study brief.md")
            # Today's brief again overwrites itself -- unless he has edited
            # it since she wrote it. Then his version stays and hers is (2).
            if os.path.exists(path) and _hash(path) != _written().get(path):
                path = path[:-3] + " (2).md"
            with open(path, "w", encoding="utf-8") as f:
                f.write(_brief_text(job, today))
            _remember_written(path)
            return path

        path = step("write", write)

        def check():
            problems = verify(path, job, today)
            if problems:
                raise ValueError("; ".join(problems))
            return True

        try:
            step("verify", check)
        except RuntimeError as exc:
            # One rewrite, then the truth.
            job["done"].pop("write", None)
            path = step("write", write)
            problems = verify(path, job, today)
            if problems:
                job["status"] = "failed"
                _save(job)
                return (f"I wrote the {module} brief but it failed my check, so don't trust it yet: "
                        f"{'; '.join(problems)[:200]}. It's at {path}.") if path else str(exc)
            job["done"]["verify"] = True

        steps = [{"text": t if module.lower() in t.lower() else f"{module}: {t}",
                  "due": f"{dt.date.fromisoformat(w):%d %b %Y} 18:00"}
                 for w, t in job["done"]["plan"]]
        existing = {t["text"].lower() for t in tasks.list_tasks()}
        steps = [s for s in steps if s["text"].lower() not in existing]
        question = planner.offer(steps)
        job["done"]["offer"] = len(steps)
        job["status"] = "done"
        _save(job)
    except RuntimeError as exc:
        return f"I couldn't finish the {module} brief: the {exc}. Nothing was added to your tasks."

    todo = [i for i in cw["items"] if not i["done"]]
    first = job["done"]["plan"][0]
    rel = os.path.relpath(path, VAULT)
    gaps = [w["week"] for w in weeks if not w["written"]]
    say = [f"Your {module} study brief is in your vault at {rel}.",
           f"Coursework is due {due:%a %d %b} ({planner._when(due, today)}), "
           f"with {len(todo)} of {len(cw['items'])} items left.",
           f"First, by {dt.date.fromisoformat(first[0]):%a %d %b}: {_lower_first(first[1])}."]
    if gaps:
        say.append(f"{_weeks_phrase(gaps)} {'isn' if len(gaps) == 1 else 'aren'}'t written up yet.")
    if job.get("resumed"):
        say.append("(Picked up where the last run stopped.)")
    return " ".join(say) + (f"\n\n{question}" if question else "")
