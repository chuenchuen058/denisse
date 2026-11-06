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
import difflib
import glob
import hashlib
import json
import os
import re
import time

import planner
import tasks

# Every phrase, alias, pattern, threshold and template lives in the JSON
# beside this file (Rule 0); this module only says how they fit together.
with open(os.path.splitext(os.path.abspath(__file__))[0] + ".json", encoding="utf-8") as _config_file:
    CONFIG = json.load(_config_file)

_P = CONFIG["patterns"]
_T = CONFIG["templates"]
_B = _T["brief"]
_M = CONFIG["matching"]
_S = CONFIG["schedule"]
_L = CONFIG["limits"]
_LEARN = CONFIG["learner"]
_TU = CONFIG["tutor"]
_PATHS = CONFIG["paths"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, *_PATHS["jobs"])
VAULT = planner.VAULT
DRAFTS = os.path.join(VAULT, *_PATHS["drafts"])
RESUME_MINUTES = CONFIG["job"]["resume_minutes"]  # an unfinished job older than this starts again

STEPS = tuple(CONFIG["steps"])
# Her question when no module was named; brain.py routes the answer back here.
WHICH_MODULE = _T["which_module"]


def _n(template: dict, count: int, **fields) -> str:
    """A template with 'one'/'many' forms, picked by count."""
    return template["one" if count == 1 else "many"].format(**fields)


# -- which module he means -------------------------------------------------
#
# Modules come from the JSON and from the vault itself (every module with a
# dated Coursework.md, via planner), so a new semester's module is known
# without editing anything. Matching is word-based throughout, so "em" in
# "them" or "system" in "operating system" doesn't count.

_discovered = {"vault": None, "at": 0.0, "modules": []}


def _vault_modules() -> list:
    """[(name, code)] for every module with dated coursework in the vault,
    re-read at most every `discovery_seconds`. Trouble reading = none."""
    now = time.monotonic()
    if _discovered["vault"] != VAULT or now - _discovered["at"] > _M["discovery_seconds"]:
        try:
            found = [(d["module"], d.get("code", "")) for d in planner.coursework_deadlines(VAULT)]
        except Exception:  # noqa: BLE001 -- the configured modules still work
            found = []
        _discovered.update(vault=VAULT, at=now, modules=found)
    return _discovered["modules"]


def _words(text: str) -> list:
    return re.sub(_M["non_word"], " ", (text or "").lower()).split()


def known_modules() -> list:
    """[{name, keys, words}] -- configured modules first, in their order, then
    any others the vault has. keys = aliases, full name and code."""
    out, by_name = [], {}
    for m in CONFIG["modules"]:
        entry = {"name": m["name"], "keys": {" ".join(_words(k)) for k in m.get("aliases", [])}}
        entry["keys"] |= {" ".join(_words(m["name"])), " ".join(_words(m.get("code", "")))}
        by_name[m["name"].lower()] = entry
        out.append(entry)
    for name, code in _vault_modules():
        entry = by_name.get(name.lower())
        if entry is None:
            entry = by_name[name.lower()] = {"name": name, "keys": {" ".join(_words(name))}}
            out.append(entry)
        entry["keys"].add(" ".join(_words(code)))
    connectors = set(_M["connectors"])
    for entry in out:
        entry["keys"].discard("")
        entry["words"] = [w for w in _words(entry["name"]) if w not in connectors]
    return out


def _exact(low: str, mod: dict) -> bool:
    return any(f" {k} " in low for k in mod["keys"])


def _prefix_run(words: list, mod: dict) -> bool:
    """'sig and sys' -> Signals and Systems: a run of his words, connectors
    allowed between, each the start of the next word of the name, in order.
    At least `min_prefix_words` of them, or the whole name if it is shorter."""
    connectors, name = set(_M["connectors"]), mod["words"]
    need = min(_M["min_prefix_words"], len(name))
    shortest = _M["min_prefix"] if need > 1 else _M["min_prefix_alone"]
    for i in range(len(words)):
        j, k, run = i, 0, 0
        while j < len(words) and k < len(name):
            w = words[j]
            if w in connectors and run:
                j += 1
                continue
            nxt = next((x for x in range(k, len(name))
                        if len(w) >= shortest and name[x].startswith(w)), None)
            if nxt is None:
                break
            run, k, j = run + 1, nxt + 1, j + 1
        if run >= need:
            return True
    return False


def _near_miss(words: list, mod: dict) -> bool:
    """'signls' -> signals: one word spelled close to a one-word alias."""
    keys = [k for k in mod["keys"] if " " not in k and len(k) >= _M["typo_min_length"]]
    return any(difflib.SequenceMatcher(None, w, k).ratio() >= _M["typo_ratio"]
               for w in words if len(w) >= _M["typo_min_length"] for k in keys)


def match_module(text: str) -> str | None:
    """The module named in what he said, or None. Tiers, strictest first: an
    alias/name/code as whole words, then word prefixes, then a near-miss
    spelling. The first tier with any hit decides; two modules = None."""
    words = _words(text)
    low = " " + " ".join(words) + " "
    modules = known_modules()
    for test in (lambda m: _exact(low, m), lambda m: _prefix_run(words, m),
                 lambda m: _near_miss(words, m)):
        hits = [m["name"] for m in modules if test(m)]
        if hits:
            return hits[0] if len(hits) == 1 else None
    return None


def _join_or(names: list) -> str:
    if len(names) < 2:
        return "".join(names)
    return _T["list_sep"].join(names[:-1]) + _T["list_or"] + names[-1]


# -- reading his notes -----------------------------------------------------

_FRONT = re.compile(_P["frontmatter"])
_BOX = re.compile(_P["checkbox"])
_QROW = re.compile(_P["question_row"])
_WK = re.compile(_P["week_ref"])
_QLABEL = re.compile(_P["question_label"])
_FEEDS = re.compile(_P["feeds_link"])
_WEEK_IN_PATH = re.compile(_P["week_in_path"])
_NEXT_SECTION = re.compile(_P["next_section"])


def _clip(text: str, n: int = CONFIG["coursework"]["topic_clip"]) -> str:
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(_T["clip_strip"]) + _T["clip_ellipsis"]


def _weeks_phrase(nums: list) -> str:
    """[2, 4, 5] -> 'Weeks 2, 4 and 5'; spoken, so no semicolons."""
    nums = [str(n) for n in nums]
    if len(nums) == 1:
        return _T["week"].format(n=nums[0])
    return _T["weeks"].format(first=_T["list_sep"].join(nums[:-1]), last=nums[-1])


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
    m = re.search(_P["section"].replace("{heading}", re.escape(heading)), text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = _NEXT_SECTION.search(rest)
    return rest[:nxt.start()] if nxt else rest


def read_coursework(path: str) -> dict:
    """Unticked work items, with marks and where to revise when the note has
    a question table (Signals does; the others are scaffolded from the
    deadline and have a Plan list instead)."""
    text = _read(path)
    questions = {}
    pipe = _P["escaped_pipe"]
    # An escaped pipe inside a cell is not a column break.
    for q, topic, marks, revise in _QROW.findall(text.replace(pipe, "\x00")):
        topic = " ".join(topic.replace("\x00", pipe[-1]).split())
        for cut in _P["topic_cut"]:
            topic = topic.split(cut)[0]
        revise = revise.replace("\x00", pipe[-1])
        questions[q] = {"topic": _clip(topic.strip()), "marks": int(marks),
                        "revise": " ".join(revise.split()),
                        "weeks": sorted({int(w) for w in _WK.findall(revise)})}
    box_src = next((s for s in (_section(text, h) for h in CONFIG["coursework"]["item_sections"]) if s), "")
    items = []
    for mark, label in _BOX.findall(box_src):
        q = _QLABEL.match(label)
        item = {"text": label, "done": mark.lower() == CONFIG["coursework"]["done_mark"]}
        if q and q.group(1) in questions:
            item.update(question=q.group(1), **questions[q.group(1)])
        items.append(item)
    feeds = sorted({int(w) for w in _FEEDS.findall(_section(text, CONFIG["coursework"]["feeds_section"]))})
    return {"items": items, "feeds": feeds, "has_questions": bool(questions)}


def read_weeks(module_dir: str) -> list:
    """Every week note: number, topic, and whether it is written up. A
    status listed under weeks.unwritten means the lecture isn't."""
    weeks = []
    for path in glob.glob(os.path.join(module_dir, *_PATHS["week_notes"])):
        fm = _front(_read(path))
        try:
            n = int(fm.get("week") or _WEEK_IN_PATH.search(path).group(1))
        except (AttributeError, ValueError):
            continue
        status = fm.get("status", "")
        weeks.append({"week": n, "topic": fm.get("topic", ""),
                      "note": os.path.splitext(os.path.basename(path))[0],
                      "written": status not in CONFIG["weeks"]["unwritten"],
                      "status": status or CONFIG["weeks"]["written_status"]})
    return sorted(weeks, key=lambda w: w["week"])


def module_tasks(module: str, code: str) -> list:
    keys = {module.lower(), code.lower()} - {""}
    return [t for t in tasks.list_tasks()
            if any(k in t["text"].lower() for k in keys)]


def _module_code(module: str) -> str:
    """The configured code for a module, '' if none."""
    return next((m.get("code", "") for m in CONFIG["modules"] if m["name"].lower() == module.lower()), "")


def _module_dir(module: str, code: str = "") -> str:
    """The module's folder in the vault (paths.module_dirs), found by its
    code or its name's words -- for a module with no coursework note to
    anchor on. '' when nothing matches."""
    want_code = (code or _module_code(module)).lower()
    want_words = [w for w in _words(module) if w not in set(_M["connectors"])]
    for path in glob.glob(os.path.join(VAULT, *_PATHS["module_dirs"])):
        if not os.path.isdir(path):
            continue
        folder = _words(os.path.basename(path))
        if (want_code and want_code in folder) or (want_words and all(w in folder for w in want_words)):
            return path
    return ""


# -- the schedule ----------------------------------------------------------

def schedule(work: list, today: dt.date, due: dt.date | None) -> list:
    """Spread the work from today to `work_ends_days_before` the deadline,
    weighted by marks (unweighted items count as `default_weight`), and keep
    the last days for assembling and submitting. Returns [(date, text)].
    With no deadline the work is listed in order, undated: [(None, text)]
    -- a date nobody gave is not spread, and nothing is submitted."""
    if due is None:
        return [(None, w["text"]) for w in work]
    default = _S["default_weight"]
    last = max(today, due - dt.timedelta(days=_S["work_ends_days_before"]))
    span = (last - today).days
    total = sum(w.get("weight", default) for w in work) or 1
    out, done = [], 0
    for w in work:
        done += w.get("weight", default)
        out.append((today + dt.timedelta(days=round(span * done / total)), w["text"]))
    out.append((max(today, due - dt.timedelta(days=_S["submit_days_before"])), _T["submit_step"]))
    return out


# -- how he learns (read-only from the learner model) ----------------------
#
# Beliefs come from learner.core -- the store, never the rendered note -- and
# only change the brief through the fixed rules below, so the same beliefs
# always give the same brief. No belief, or none a rule recognises: the brief
# is exactly what it was before the learner model existed. Nothing here
# writes to the store or infers a belief from what the brief says.

_DURATION = re.compile(_P["duration"])


def _format_rules() -> dict:
    """learner.format_first, read at call time: kind -> its rule. A kind
    added in the JSON (pattern, shape, optional revise_first, and its brief
    lines under templates.brief) works with no code change."""
    return _LEARN["format_first"]


def _format_shapes() -> list:
    """The shapes a format belief can call for, in JSON order; null = none."""
    return list(dict.fromkeys(r["shape"] for r in _format_rules().values() if r.get("shape")))


def _revise_first() -> dict:
    """shape -> the pattern of revise-from sources it moves to the front."""
    out = {}
    for rule in _format_rules().values():
        if rule.get("shape") and rule.get("revise_first"):
            out.setdefault(rule["shape"], rule["revise_first"])
    return out


def learner_beliefs() -> list:
    """Current beliefs from the learner store, in store order. Any trouble
    reading it means no beliefs -- the brief never fails because of it."""
    try:
        from learner import core as learner_core
        data = learner_core.load()
    except Exception:  # noqa: BLE001 -- a missing or broken store = today's brief
        return []
    return [b for b in data.get("beliefs", [])
            if isinstance(b, dict) and b.get("status") == "current" and b.get("id")]


def _unit_minutes(unit: str) -> float:
    unit = unit.lower()
    return next((m for prefix, m in _LEARN["unit_minutes"].items() if unit.startswith(prefix)),
                _LEARN["default_unit_minutes"])


def _session_minutes(claim: str) -> int | None:
    found = [float(n) * _unit_minutes(u) for n, u in _DURATION.findall(claim)]
    return int(min(found)) if found else None


def _per_session(minutes: int) -> int:
    return next((per for upto, per in _LEARN["pace"] if minutes <= upto), _LEARN["pace_max"])


def _format_first(claim: str) -> str | None:
    """Which configured format the claim names first, if any."""
    hits = [(m.start(), kind) for kind, rule in _format_rules().items()
            for m in [re.search(rule["pattern"], claim)] if m]
    return min(hits)[1] if hits else None


def learner_shape(beliefs: list) -> dict:
    """The adaptations these beliefs call for. Areas: `format` (worked example
    or diagram before theory), `attention` (shorter sections), `study` (his
    session length = pace: how many plan items one session covers). Other
    areas, and claims no rule recognises, change nothing and aren't listed."""
    areas, rules = _LEARN["areas"], _format_rules()
    shape = dict.fromkeys([*_format_shapes(), "short", "pace"])
    for b in beliefs:
        area, claim = b.get("area"), str(b.get("claim", ""))
        if area == areas["format"]:
            kind = _format_first(claim)
            key = rules[kind].get("shape") if kind else None
            if key and not shape[key]:
                shape[key] = b
        elif area == areas["attention"] and claim.strip() and not shape["short"]:
            shape["short"] = b
        elif area == areas["study"]:
            mins = _session_minutes(claim)
            if mins:  # the latest stated length wins
                shape["pace"] = {**b, "minutes": mins, "per_session": _per_session(mins)}
    return {k: v for k, v in shape.items() if v}


def _sources_first(revise: str, pattern: str) -> str:
    """'Wk 4 lecture; Wk 4 tutorial' -> 'Wk 4 tutorial; Wk 4 lecture' when
    the pattern names tutorials: matching sources first, order otherwise kept."""
    sep = _P["revise_sep"]
    parts = [p.strip() for p in revise.split(sep)]
    return (sep + " ").join([p for p in parts if re.search(pattern, p)]
                            + [p for p in parts if not re.search(pattern, p)])


def _shaped_by(shape: dict) -> list:
    """The footer: one line per adaptation used, in templates.brief.shaped order."""
    def cite(b):
        return _B["cite"].format(id=b["id"], claim=_clip(str(b.get("claim", "")), _LEARN["claim_clip"]))
    lines = list(_B["shaped_heading"])
    for key, line in _B["shaped"].items():
        if key in shape:
            b = shape[key]
            lines.append(line.format(cite=cite(b), per=b.get("per_session"), minutes=b.get("minutes")))
    lines.append(_B["shaped_fix"])
    return lines


# -- the study tutor's weak topics (read-only) -----------------------------
#
# tutor/progress.py (python -m tutor progress) records, per module, the
# topics Anki says he keeps losing and the tutor notes to revise them from.
# The brief puts those first. No progress run, nothing weak, or any trouble
# reading it: {} -- and the brief is exactly what it was without the tutor.

def tutor_focus(module: str) -> dict:
    """{tutor, weak: [{topic, weeks, coursework, notes: [(week, stem)]}],
    tutor_notes: {week: stem}} from the tutor's last progress run, or {}."""
    try:
        from tutor import progress
        return progress.focus(module, VAULT) or {}
    except Exception:  # noqa: BLE001 -- a brief never fails because of the tutor
        return {}


def _norm(text: str) -> str:
    return " ".join(str(text).split()).lower()


def weak_item(item: dict, focus: dict) -> bool:
    """A coursework item a weak topic feeds: the topic map names it, or it
    is revised from one of the topic's weeks."""
    for w in (focus or {}).get("weak", []):
        if _norm(item.get("text", "")) in {_norm(c) for c in w.get("coursework", [])}:
            return True
        if set(item.get("weeks", [])) & set(w.get("weeks", [])):
            return True
    return False


def prioritise(items: list, focus: dict) -> list:
    """Items a weak topic feeds first, the rest after, each in its order."""
    return ([i for i in items if weak_item(i, focus)]
            + [i for i in items if not weak_item(i, focus)])


def revise_steps(focus: dict) -> list:
    """One plan step per weak topic, worst first, at most revise_max."""
    out = []
    for w in (focus or {}).get("weak", [])[:_TU["revise_max"]]:
        weeks = (_TU["revise_weeks"].format(weeks=_TU["weeks_sep"].join(str(n) for n in w["weeks"]))
                 if w.get("weeks") else "")
        out.append({"text": _TU["revise_step"].format(topic=w["topic"], weeks=weeks, tutor=focus.get("tutor", "")),
                    "weight": _TU["revise_weight"]})
    return out


def _tutor_lines(focus: dict, gaps: list) -> tuple:
    """(where-you-are lines: the tutor notes for unwritten weeks; the
    weak-topics section). Both empty with no focus."""
    if not focus:
        return [], []
    where, lines = [], []
    notes = {int(k): v for k, v in (focus.get("tutor_notes") or {}).items()}
    cited = [_TU["link"].format(stem=notes[w["week"]], week=w["week"]) for w in gaps if w["week"] in notes]
    if cited:
        where.append(_TU["tutor_notes"].format(tutor=focus.get("tutor", ""), links=_TU["link_sep"].join(cited)))
    weak = focus.get("weak", [])
    if weak:
        lines += _TU["heading"]
        for w in weak:
            links = _TU["link_sep"].join(_TU["link"].format(stem=s, week=n) for n, s in w.get("notes", []))
            lines.append(_TU["weak"].format(
                topic=w["topic"], tutor=focus.get("tutor", ""),
                links=_TU["links"].format(links=links) if links else "",
                coursework=_TU["coursework"].format(items=_TU["item_sep"].join(w["coursework"]))
                if w.get("coursework") else ""))
    return where, lines


# -- the job ---------------------------------------------------------------

def _job_file(module: str) -> str:
    sep = _PATHS["slug_sep"]
    slug = re.sub(_P["slug"], sep, module.lower()).strip(sep)
    return os.path.join(JOBS_DIR, _PATHS["job_file"].format(slug=slug))


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


def _planned_write_ups(plan: list) -> set:
    rx = re.compile(_P["planned_write_up"])
    return {int(m) for _, t in plan for m in rx.findall(t)}


def _brief_text(job: dict, today: dt.date, shape: dict | None = None) -> str:
    shape = shape or {}
    short = "short" in shape
    d, cw, weeks = job["done"]["deadline"], job["done"]["coursework"], job["done"]["weeks"]
    due = dt.date.fromisoformat(d["due"]) if d.get("due") else None
    mod = job["module"]
    todo = [i for i in cw["items"] if not i["done"]]
    total = len(cw["items"])
    by_week = {w["week"]: w for w in weeks}
    due_text = _B["due_text"].format(due=due) if due else _B["due_unknown_text"]
    lines = [line.format(today=today, module=mod, due_text=due_text, left=len(todo), total=total)
             for line in _B["front"]]
    if due:
        cw_link = os.path.relpath(d["path"], VAULT).replace(os.sep, "/")
        lines += [_B["due"].format(due=due, when=planner._when(due, today))
                  + (_B["weight"].format(weight=d["weight"]) if d["weight"] else ""),
                  _B["source"].format(link=cw_link, today=today)]
    else:
        # No dated coursework: say so in the brief, never a guessed date.
        lines += [_B["due_unknown"].format(module=mod), _B["source_undated"].format(today=today)]
    lines += _B["where_heading"]
    lines.append(_B["ticked"].format(ticked=total - len(todo), total=total))
    if not due and weeks:
        lines.append(_B["weeks_found"].format(weeks=_T["list_sep"].join(
            _B["week_found"].format(note=w["note"], week=w["week"], status=w["status"]) for w in weeks)))
    if d["brief_unread"]:
        lines.append(_B["brief_unread"])
    gaps = [w for w in weeks if not w["written"]]
    if gaps:
        lines.append(_B["gaps"].format(weeks=_T["list_sep"].join(
            _B["gap"].format(note=w["note"], week=w["week"])
            + ("" if short else _B["gap_status"].format(status=w["status"])) for w in gaps)))
    tutor_where, tutor_lines = _tutor_lines(job["done"].get("tutor") or {}, gaps)
    lines += tutor_where
    found = job["done"]["tasks"]
    if found and short:
        lines.append(_B["tasks_short"].format(count=len(found)))
    elif found:
        lines.append(_B["tasks"].format(count=len(found), preview=_B["task_sep"].join(
            t["text"][:_L["task_preview_chars"]] for t in found[:_L["task_preview"]])))
    lines += tutor_lines
    lines += _B["left_heading"]
    for key in _format_shapes():
        if key in shape:
            lines += _B.get(key, [])
    reorder = [rx for key, rx in _revise_first().items() if key in shape]
    if cw["has_questions"]:
        lines += _B["table_head"]
        for i in todo:
            revise = i.get('revise', '')
            for rx in reorder:
                revise = _sources_first(revise, rx)
            lines.append(_B["table_row"].format(text=i["text"], marks=i.get("marks", ""), revise=revise))
    else:
        lines += [_B["list_row"].format(text=i["text"]) for i in todo]
    needed = {w for i in todo for w in i.get("weeks", [])}
    if not cw["has_questions"]:
        needed |= set(cw["feeds"])  # no table: every week the note says feeds it
    missing = sorted(w for w in needed if w in by_week and not by_week[w]["written"])
    lines += _B["plan_heading"]
    planned = _planned_write_ups(job["done"]["plan"])
    if missing:
        listed = [w for w in missing if w not in planned]
        note = _n(_B["missing"], len(missing), weeks=_weeks_phrase(missing))
        if short:
            note = _n(_B["missing_short"], len(missing), weeks=_weeks_phrase(missing))
        elif listed:
            note += _n(_B["missing_listed"], len(listed), weeks=_weeks_phrase(listed))
        lines += [note, ""]
    if not job["done"]["plan"]:
        lines.append(_B["nothing_planned"])
    for when, text in job["done"]["plan"]:
        if when:
            lines.append(_B["plan_row"].format(when=dt.date.fromisoformat(when), text=text))
        else:
            lines.append(_B["plan_row_undated"].format(text=text))
    if "pace" in shape and due:
        per, plan = shape["pace"]["per_session"], job["done"]["plan"]
        lines += _B["sessions_heading"]
        lines += [_n(_B["sessions"], per, minutes=shape["pace"]["minutes"], per=per), ""]
        for n, k in enumerate(range(0, len(plan), per), 1):
            group = plan[k:k + per]
            lines.append(_B["session_row"].format(n=n, when=dt.date.fromisoformat(group[-1][0]),
                                                  items=_B["session_sep"].join(t for _, t in group)))
    untaught = [i["text"] for i in todo if i.get("question") and not i.get("weeks")]
    lines += _B["unknown_heading"]
    if untaught:
        lines.append(_B["untaught"].format(items=_B["untaught_sep"].join(
            t[:_L["untaught_chars"]] for t in untaught)))
    lines.append(_B["unknown"])
    if shape:
        lines += _shaped_by(shape)
    return "\n".join(lines) + "\n"


_LINK = re.compile(_P["wikilink"])


def verify(path: str, job: dict, today: dt.date) -> list:
    """Problems with the written brief, read back from disk. Empty = good."""
    say = _T["problems"]
    try:
        text = _read(path)
    except OSError as exc:
        return [say["unreadable"].format(error=exc)]
    problems = []
    d = job["done"]["deadline"]
    due = dt.date.fromisoformat(d["due"]) if d.get("due") else None
    if due and _T["verify_due"].format(due=due) not in text:
        problems.append(say["no_due"])
    for i in job["done"]["coursework"]["items"]:
        if not i["done"] and i["text"] not in text:
            problems.append(say["missing_item"].format(item=i["text"][:_L["missing_item_chars"]]))
    for when, _ in job["done"]["plan"]:
        if due and when and not today <= dt.date.fromisoformat(when) <= due:
            problems.append(say["plan_date"].format(when=when))
    names = {os.path.splitext(os.path.basename(p))[0].lower()
             for p in glob.glob(os.path.join(VAULT, "**", "*" + _PATHS["note_ext"]), recursive=True)}
    for target in _LINK.findall(text):
        target = target.strip()
        if "/" in target:
            if not os.path.exists(os.path.join(VAULT, target)):
                problems.append(say["broken_link"].format(target=target))
        elif target.lower() not in names:
            problems.append(say["broken_link"].format(target=target))
    return problems


def _hash(path: str) -> str:
    try:
        return hashlib.sha256(_read(path).encode("utf-8")).hexdigest()
    except OSError:
        return ""


# What she last wrote to each brief, across runs -- so a brief he has edited
# since is recognised as his and never overwritten.
def _written_file() -> str:
    return os.path.join(JOBS_DIR, _PATHS["written_file"])


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
        return _T["which_module_offer"].format(
            question=WHICH_MODULE, modules=_join_or([m["name"] for m in known_modules()]))
    job = _load(module, now)

    def step(name, fn):
        if name in job["done"]:
            return job["done"][name]  # finished in an earlier, interrupted run
        for _attempt in range(CONFIG["job"]["attempts"]):
            try:
                job["done"][name] = fn()
                job["errors"].pop(name, None)
                _save(job)
                return job["done"][name]
            except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
                job["errors"][name] = f"{type(exc).__name__}: {exc}"
        job["status"] = "failed"
        _save(job)
        raise RuntimeError(_T["step_failed"].format(step=name, detail=job["errors"][name].split(": ", 1)[-1]))

    try:
        def find_deadline():
            hits = [d for d in planner.coursework_deadlines(VAULT)
                    if d["module"] == module and d["due"] >= today]
            if hits:
                d = hits[0]
                return {**d, "due": d["due"].isoformat()}
            # No dated coursework (2026-11-04: "prep me for electromagnetism"
            # failed 6/6 on this). The brief is still worth having -- the
            # week notes, what's written up -- with the date said unknown,
            # never guessed. The module's folder stands in for the note.
            folder = _module_dir(module)
            if not folder:
                raise LookupError(_T["no_notes"].format(module=module))
            return {"module": module, "code": _module_code(module), "due": None, "weight": "",
                    "brief_unread": False, "path": "", "folder": folder}

        d = step("deadline", find_deadline)
        due = dt.date.fromisoformat(d["due"]) if d["due"] else None
        cw = step("coursework", lambda: read_coursework(d["path"]) if d["path"]
                  else {"items": [], "feeds": [], "has_questions": False})
        if d["path"]:
            module_dir = os.path.dirname(d["path"])
            if os.path.basename(module_dir).lower() == _PATHS["coursework_dir"]:
                module_dir = os.path.dirname(module_dir)
        else:
            module_dir = d["folder"]
        weeks = step("weeks", lambda: read_weeks(module_dir))
        step("tasks", lambda: [{"text": t["text"], "due": str(t.get("due") or "")}
                               for t in module_tasks(module, d.get("code", ""))])
        focus = step("tutor", lambda: tutor_focus(module))

        def make_plan():
            by_week = {w["week"]: w for w in weeks}
            work, written_up = [], set()
            listed = " | ".join(t["text"].lower() for t in job["done"]["tasks"])
            skip = re.compile(_P["skip_item"])

            def write_up(w):
                if re.search(_P["write_up_task"].replace("{week}", re.escape(str(w))), listed):
                    written_up.add(w)  # already on his task list -- don't add it twice
                if w in by_week and not by_week[w]["written"] and w not in written_up:
                    written_up.add(w)
                    work.append({"text": _T["write_up_step"].format(
                        week=w, topic=by_week[w]["topic"] or _T["write_up_topic"]),
                        "weight": _S["write_up_weight"]})

            if d["brief_unread"]:
                work.append({"text": _T["read_brief_step"].format(module=module),
                             "weight": _S["read_brief_weight"]})
            work.extend(revise_steps(focus))  # weak topics first (the study tutor)
            if not cw["has_questions"]:
                # No question table to say which week each part needs: write
                # up every gap among the weeks the note says feed it, first.
                # No coursework note at all: every week not written up.
                for w in (cw["feeds"] if d["path"] else sorted(by_week)):
                    write_up(w)
            for i in prioritise(cw["items"], focus):
                if i["done"] or skip.match(i["text"]):
                    continue
                for w in i.get("weeks", []):
                    write_up(w)
                label = (_T["question_step"].format(text=i["text"], topic=i["topic"], marks=i["marks"])
                         if i.get("marks") else i["text"])
                work.append({"text": label, "weight": i.get("marks", _S["default_weight"])})
            return [(when.isoformat() if when else None, t) for when, t in schedule(work, today, due)]

        job["done"].setdefault("plan", None)
        if job["done"]["plan"] is None:
            job["done"]["plan"] = make_plan()
            _save(job)

        def write():
            os.makedirs(DRAFTS, exist_ok=True)
            path = os.path.join(DRAFTS, _PATHS["draft_name"].format(today=today, module=module))
            # Today's brief again overwrites itself -- unless he has edited
            # it since she wrote it. Then his version stays and hers is a copy.
            if os.path.exists(path) and _hash(path) != _written().get(path):
                stem, ext = os.path.splitext(path)
                path = stem + _PATHS["draft_copy_suffix"] + ext
            with open(path, "w", encoding="utf-8") as f:
                f.write(_brief_text(job, today, learner_shape(learner_beliefs())))
            _remember_written(path)
            return path

        path = step("write", write)

        def check():
            problems = verify(path, job, today)
            if problems:
                raise ValueError(_T["problems"]["sep"].join(problems))
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
                return _T["failed_check"].format(
                    module=module, path=path,
                    problems=_T["problems"]["sep"].join(problems)[:_L["failure_chars"]]) if path else str(exc)
            job["done"]["verify"] = True

        steps = [{"text": t if module.lower() in t.lower() else _T["task_prefix"].format(module=module, text=t),
                  "due": _S["task_due"].format(when=dt.date.fromisoformat(w)) if w else ""}
                 for w, t in job["done"]["plan"]]
        existing = {t["text"].lower() for t in tasks.list_tasks()}
        steps = [s for s in steps if s["text"].lower() not in existing]
        question = planner.offer(steps)
        job["done"]["offer"] = len(steps)
        job["status"] = "done"
        _save(job)
    except RuntimeError as exc:
        return _T["could_not_finish"].format(module=module, error=exc)

    todo = [i for i in cw["items"] if not i["done"]]
    plan = job["done"]["plan"]
    rel = os.path.relpath(path, VAULT)
    gaps = [w["week"] for w in weeks if not w["written"]]
    reply = _T["reply"]
    say = [reply["where"].format(module=module, path=rel)]
    if due:
        say.append(reply["due"].format(due=due, when=planner._when(due, today), left=len(todo), total=len(cw["items"])))
    else:
        say.append(reply["due_unknown"].format(module=module))
    if not plan:
        say.append(reply["nothing_planned"])
    elif plan[0][0]:
        say.append(reply["first"].format(when=dt.date.fromisoformat(plan[0][0]), text=_lower_first(plan[0][1])))
    else:
        say.append(reply["first_undated"].format(text=_lower_first(plan[0][1])))
    if gaps:
        say.append(_n(reply["gaps"], len(gaps), weeks=_weeks_phrase(gaps)))
    if job.get("resumed"):
        say.append(reply["resumed"])
    return reply["sep"].join(say) + (reply["question"].format(question=question) if question else "")
