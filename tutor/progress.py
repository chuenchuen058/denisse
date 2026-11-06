"""python -m tutor progress: where Chuen is, per module (study tutor part 4).

For every module folder in the vault:

    lectures    weeks the KEATS inbox holds (what the last ingest placed,
                plus files downloaded since, placed by folder and name
                only -- nothing is extracted here) against the weeks that
                have a tutor note
    topics      the topic map, each with its Anki numbers (part 2's stats)
                and a mastery figure: reviewed share x retention
    weak        the stats' weak topics, worst first, with the tutor notes
                to revise from and the coursework they feed
    coursework  every coursework note in the module folder: due date
                (planner's reader), items left (study_brief's reader)
    next        what to study next, in the order progress.next.order says

Written as:

    <progress.note>                 the vault overview; only the block between
                                    progress.block_start/end is Don's, his
                                    edits outside it are kept
    <Don data>/progress-<slug>.json one module, in full (study_brief reads it)
    <Don data>/<dataset_file>       one row per module, the Atrium hub's table

Weak topics also go to the learner model (don.feed_learner, idempotent).
Nothing here calls a model; every line comes from tutor.json "progress".
"""
from __future__ import annotations

import datetime as dt
import os
import re

from . import cards, conf, don, extract, ingest, notes, topics
from .conf import CONFIG, Module, Paths

_PR = CONFIG["progress"]
_N = CONFIG["note"]
_SAY = CONFIG["say"]
_SEP = _N["list_sep"]


def _fmt(tpl: str, **kw) -> str:
    return tpl.format(**{**don.ctx(), **kw})


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _weeks_text(weeks) -> str:
    return _SEP.join(str(w) for w in sorted(weeks))


# -- what there is -------------------------------------------------------------------

def lectures(paths: Paths, module: Module, state: dict, modules: list) -> dict:
    """Weeks on KEATS (from the inbox) against weeks read into a tutor note."""
    on_keats, new_files = set(), 0
    for rec in state["files"].values():
        if rec.get("status") == "matched" and rec.get("module") == module.slug and rec.get("week"):
            on_keats.add(int(rec["week"]))
    for folder, home in ingest.inboxes(paths, modules):
        for path, parts in ingest.scan(folder):
            if extract.sha256(path) in state["files"]:
                continue
            placed, week, _why = ingest.match(path, parts, home, modules, lambda _p: "")
            if placed is not None and placed.slug == module.slug and week:
                on_keats.add(int(week))
                new_files += 1
    read = set()
    for week, rec in state["lectures"].get(module.slug, {}).items():
        if rec.get("note") and os.path.isfile(rec.get("written") or rec["note"]):
            read.add(int(week))
    return {"on_keats": sorted(on_keats | read), "read": sorted(read),
            "unread": sorted(on_keats - read), "new_files": new_files}


def mastery(t: dict) -> int | None:
    """Percent: share of the topic's cards reviewed, times how well they are
    kept (lapses per reviewed card, progress.mastery.lapse_rate_zero = none
    kept). None when nothing has been reviewed."""
    if not t.get("cards") or not t.get("reviewed"):
        return None
    coverage = t["reviewed"] / t["cards"]
    retention = max(0.0, 1 - (t["lapses"] / t["reviewed"]) / _PR["mastery"]["lapse_rate_zero"])
    return round(100 * coverage * retention)


def _module_mastery(rows: list) -> int | None:
    rated = [t for t in rows if t.get("mastery") is not None]
    total = sum(t["cards"] for t in rated)
    return round(sum(t["mastery"] * t["cards"] for t in rated) / total) if total else None


def topic_rows(paths: Paths, module: Module, topic_map: dict, stats: dict) -> list:
    """The topic map's topics, then any topic only the cards know, each with
    its Anki numbers (zeros before any sync)."""
    by_name = {}
    for t in topic_map.get("topics", []):
        by_name[t["name"].lower()] = {"topic": t["name"], "weeks": list(t.get("lectures", [])),
                                      "coursework": list(t.get("coursework", []))}
    for card in cards.checked_cards(cards.load(paths, module)):
        name = (card.get("topic") or "").strip()
        if not name:
            continue
        rec = by_name.setdefault(name.lower(), {"topic": name, "weeks": [], "coursework": []})
        rec["deck_cards"] = rec.get("deck_cards", 0) + 1
        if card.get("week") and card["week"] not in rec["weeks"]:
            rec["weeks"].append(card["week"])
    for s in stats.get("topics", []):
        rec = by_name.setdefault(s["topic"].lower(), {"topic": s["topic"], "weeks": [], "coursework": []})
        rec.update({k: s.get(k) for k in ("cards", "reviewed", "lapses", "ease", "due", "weak", "score")})
        rec["weeks"] = sorted(set(rec["weeks"]) | set(s.get("weeks", [])))
    out = []
    for rec in by_name.values():
        rec.setdefault("cards", rec.get("deck_cards", 0))
        for key in ("reviewed", "lapses", "due"):
            rec[key] = rec.get(key) or 0
        rec["weak"] = bool(rec.get("weak"))
        rec["score"] = rec.get("score") or 0
        rec.pop("deck_cards", None)
        rec["weeks"] = sorted(rec["weeks"])
        rec["mastery"] = mastery(rec)
        out.append(rec)
    out.sort(key=lambda r: (r["weeks"][0] if r["weeks"] else float("inf"), r["topic"].lower()))
    return out


def coursework(module: Module, vault: str, today: dt.date) -> list:
    """Every coursework note in the module folder: its due date (planner's
    reader, None when it has none) and the items still unticked."""
    import glob

    import planner
    import study_brief
    try:
        dated = {os.path.normcase(os.path.abspath(d["path"])): d for d in planner.coursework_deadlines(vault)}
    except Exception:  # noqa: BLE001 -- undated is still worth listing
        dated = {}
    out, seen = [], set()
    for pattern in CONFIG["paths"]["coursework_globs"]:
        for path in sorted(glob.glob(os.path.join(glob.escape(module.folder), pattern))):
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            try:
                cw = study_brief.read_coursework(path)
            except Exception:  # noqa: BLE001 -- a note it can't read is listed with no items
                cw = {"items": []}
            due = dated.get(key, {}).get("due")
            left = [i for i in cw["items"] if not i["done"]]
            out.append({"note": os.path.relpath(path, vault).replace(os.sep, "/"), "stem": _stem(path),
                        "due": due.isoformat() if due else None,
                        "days": (due - today).days if due else None,
                        "weight": dated.get(key, {}).get("weight", ""),
                        "left": [i["text"] for i in left], "total": len(cw["items"]),
                        "weeks": sorted({w for i in left for w in i.get("weeks", [])})})
    out.sort(key=lambda c: (c["due"] is None, c["due"] or ""))
    return out


def _tutor_notes(module: Module, weeks) -> list:
    """[(week, stem)] for each week with a tutor note on disk."""
    out = []
    for w in sorted(set(weeks)):
        path = notes.note_path(module, w)
        if os.path.isfile(path):
            out.append((w, _stem(path)))
    return out


def _practice(paths: Paths, module: Module) -> dict:
    from . import exam
    history = (don.load_json(paths, exam.sidecar(module)) or {}).get("history", [])
    exams = [h for h in history if h.get("kind") == exam.KIND_EXAM and h.get("total")]
    quizzes = [h for h in history if h.get("kind") == exam.KIND_QUIZ]
    return {"exams": len(exams),
            "exam_mean": round(100 * sum(h["marks"] / h["total"] for h in exams) / len(exams)) if exams else None,
            "quizzes": len(quizzes)}


# -- what to do next -------------------------------------------------------------------

def _soonest(cw: list, labels: list, weeks: list) -> dict | None:
    """The soonest dated coursework with an unticked item this topic feeds:
    named by the topic map, or revised from one of its weeks."""
    norm = {" ".join(x.split()).lower() for x in labels}
    for c in cw:
        if c["due"] is None or c["days"] < 0:
            continue
        if any(" ".join(i.split()).lower() in norm for i in c["left"]) or set(weeks) & set(c["weeks"]):
            return c
    return None


def study_next(module: Module, rec: dict) -> list:
    """[{kind, text}] in progress.next.order, at most next.max."""
    nx = _PR["next"]
    out = {}
    lec, cw = rec["lectures"], rec["coursework"]
    if lec["unread"] or lec["new_files"]:
        out.setdefault("ingest", []).append(_fmt(nx["ingest"], weeks=_weeks_text(lec["unread"]) or "-",
                                                 n=lec["new_files"], module=module.title))
    for w in rec["weak"]:
        links = _SEP.join(_fmt(nx["week_link"], stem=s, week=n) for n, s in w["notes"]) or _weeks_text(w["weeks"])
        c = _soonest(cw, w["coursework"], w["weeks"])
        if c is not None:
            out.setdefault("weak_coursework", []).append(_fmt(
                nx["weak_coursework"], topic=w["topic"], links=links or "-", due=c["due"], days=c["days"],
                coursework=_fmt(nx["coursework_link"], note=c["note"][:-len(CONFIG["sources"]["note_ext"])],
                                stem=c["stem"])))
        else:
            out.setdefault("weak", []).append(_fmt(nx["weak"], topic=w["topic"], links=links or "-"))
    for c in cw:
        if c["due"] and 0 <= c["days"] <= _PR["horizon_days"] and c["left"]:
            out.setdefault("deadline", []).append(_fmt(
                nx["deadline"], n=len(c["left"]), total=c["total"], due=c["due"], days=c["days"],
                coursework=_fmt(nx["coursework_link"], note=c["note"][:-len(CONFIG["sources"]["note_ext"])],
                                stem=c["stem"])))
    due = sum(t["due"] for t in rec["topics"])
    if due:
        out.setdefault("due", []).append(_fmt(nx["due"], n=due, module=module.title))
    fresh = [t for t in rec["topics"] if t["cards"] and not t["reviewed"]]
    if fresh:
        out.setdefault("unreviewed", []).append(_fmt(nx["unreviewed"], n=len(fresh),
                                                     topics=_SEP.join(t["topic"] for t in fresh[:nx["topics_max"]])))
    bare = [t for t in rec["topics"] if not t["cards"]]
    if bare:
        out.setdefault("uncarded", []).append(_fmt(nx["uncarded"], n=len(bare), module=module.title,
                                                   topics=_SEP.join(t["topic"] for t in bare[:nx["topics_max"]])))
    items = [{"kind": k, "text": text} for k in nx["order"] for text in out.get(k, [])]
    return items[:nx["max"]]


# -- one module, all modules -------------------------------------------------------------

def build(paths: Paths, module: Module, state: dict, modules: list, today: dt.date) -> dict:
    topic_map = topics.load(paths, module) or {}
    stats = don.load_json(paths, CONFIG["don"]["anki_file"].format(slug=module.slug)) or {}
    rows = topic_rows(paths, module, topic_map, stats)
    cw = coursework(module, paths.vault, today)
    weak = []
    for t in sorted((t for t in rows if t["weak"]), key=lambda t: -t["score"]):
        weak.append({"topic": t["topic"], "weeks": t["weeks"], "score": t["score"], "lapses": t["lapses"],
                     "reviewed": t["reviewed"], "coursework": t["coursework"],
                     "notes": _tutor_notes(module, t["weeks"])})
    rec = {"module": module.title, "code": module.code, "slug": module.slug, "updated": today.isoformat(),
           "synced": stats.get("synced"), "lectures": lectures(paths, module, state, modules),
           "topics": rows, "weak": weak, "coursework": cw, "practice": _practice(paths, module),
           "cards": sum(t["cards"] for t in rows), "reviewed": sum(t["reviewed"] for t in rows),
           "mastery": _module_mastery(rows),
           "tutor_notes": _tutor_notes(module, [int(w) for w in state["lectures"].get(module.slug, {})])}
    rec["next"] = study_next(module, rec)
    return rec


def hub_row(rec: dict) -> dict:
    """One flat row for the Atrium hub's study table."""
    lec = rec["lectures"]
    dated = [c for c in rec["coursework"] if c["due"] and c["days"] >= 0]
    c = dated[0] if dated else None
    return {"module": rec["module"], "code": rec["code"], "lectures_read": len(lec["read"]),
            "lectures_on_keats": len(lec["on_keats"]), "lectures_unread": _weeks_text(lec["unread"]),
            "topics": len(rec["topics"]), "cards": rec["cards"], "reviewed": rec["reviewed"],
            "mastery": rec["mastery"], "weak": _SEP.join(w["topic"] for w in rec["weak"]),
            "next_deadline": c["due"] if c else None, "days_left": c["days"] if c else None,
            "coursework_left": len(c["left"]) if c else None,
            "next": rec["next"][0]["text"] if rec["next"] else "", "updated": rec["updated"]}


def note_path(paths: Paths) -> str:
    return os.path.join(paths.vault, *_PR["note"])


def module_file(module: Module) -> str:
    return _PR["module_file"].format(slug=module.slug)


def _blank(value, tpl_key: str = "none") -> str:
    return _PR["table"][tpl_key] if value is None else str(value)


def render_block(paths: Paths, recs: list, today: dt.date) -> list:
    t = _PR["table"]
    lines = [_fmt(line, today=today.isoformat()) for line in _PR["block_intro"]]
    if not recs:
        return lines + [_fmt(_PR["none"])]
    lines += list(t["overview_head"])
    for rec in recs:
        lec = rec["lectures"]
        dated = [c for c in rec["coursework"] if c["due"] and c["days"] >= 0]
        progress_note = don.progress_path(paths, Module(rec["folder"], rec["code"], rec["module"]))
        name = _fmt(t["module_link"], stem=_stem(progress_note), module=rec["module"]) \
            if os.path.isfile(progress_note) else rec["module"]
        lines.append(_fmt(
            t["overview_row"], module=name, read=len(lec["read"]), on_keats=len(lec["on_keats"]),
            topics=len(rec["topics"]), mastery=_blank(rec["mastery"]), weak=len(rec["weak"]),
            deadline=_fmt(t["deadline"], due=dated[0]["due"], days=dated[0]["days"]) if dated else t["none"]))
    for rec in recs:
        module = Module(rec["folder"], rec["code"], rec["module"])
        lec = rec["lectures"]
        lines += [""] + [_fmt(x, module=rec["module"], code=rec["code"]) for x in _PR["module_heading"]]
        lines.append(_fmt(_PR["lectures"], read=_weeks_text(lec["read"]) or t["none"],
                          n=len(lec["read"]), on_keats=len(lec["on_keats"])))
        if lec["unread"]:
            lines.append(_fmt(_PR["lectures_unread"], weeks=_weeks_text(lec["unread"])))
        if lec["new_files"]:
            lines.append(_fmt(_PR["lectures_new"], n=lec["new_files"]))
        for c in rec["coursework"]:
            link = _fmt(_PR["next"]["coursework_link"], note=c["note"][:-len(CONFIG["sources"]["note_ext"])],
                        stem=c["stem"])
            if c["due"]:
                lines.append(_fmt(_PR["coursework"], coursework=link, due=c["due"], days=c["days"],
                                  left=len(c["left"]), total=c["total"]))
            else:
                lines.append(_fmt(_PR["coursework_undated"], coursework=link, left=len(c["left"]), total=c["total"]))
        if not rec["coursework"]:
            lines.append(_fmt(_PR["coursework_none"]))
        p = rec["practice"]
        if p["exams"] or p["quizzes"]:
            lines.append(_fmt(_PR["practice"], exams=p["exams"], mean=_blank(p["exam_mean"]), quizzes=p["quizzes"]))
        lines += [""] + list(t["topics_head"])
        for row in rec["topics"]:
            weeks = _SEP.join(_fmt(t["week_link"], stem=s, week=w) for w, s in _tutor_notes(module, row["weeks"]))
            lines.append(_fmt(t["topics_row"], topic=row["topic"], weeks=weeks or _weeks_text(row["weeks"]) or t["none"],
                              cards=row["cards"], reviewed=row["reviewed"], lapses=row["lapses"],
                              mastery=_blank(row["mastery"]), weak=t["weak_mark"] if row["weak"] else ""))
        if not rec["topics"]:
            lines.append(_fmt(_PR["topics_none"], module=rec["module"]))
        lines += [""] + [_fmt(x) for x in _PR["next_heading"]]
        for n, item in enumerate(rec["next"], 1):
            lines.append(_fmt(_PR["next_item"], n=n, text=item["text"]))
        if not rec["next"]:
            lines.append(_fmt(_PR["next_none"]))
    return lines


def _with_updated(front: str, today: dt.date) -> str:
    return re.sub(_PR["updated_line"], lambda m: m.group(1) + today.isoformat(), front, count=1)


def render_note(paths: Paths, recs: list, today: dt.date, existing: str | None) -> str:
    """The whole note: his text kept, only the generated block replaced."""
    start, end = _fmt(_PR["block_start"]), _fmt(_PR["block_end"])
    block = "\n".join([start] + render_block(paths, recs, today) + [end])
    if existing is None:
        head = "\n".join(_fmt(x, today=today.isoformat(), name=_fmt(_PR["name"]), hub=don.hub_link(paths))
                         for x in _PR["front"])
        return head + "\n" + block + "\n"
    a, b = existing.find(start), existing.find(end)
    if a != -1 and b > a:
        text = existing[:a] + block + existing[b + len(end):]
    else:
        text = existing.rstrip("\n") + "\n\n" + block + "\n"
    fm = re.match(_PR["frontmatter"], text)
    if fm:
        text = _with_updated(fm.group(0), today) + text[fm.end():]
    return text


def write_note(paths: Paths, recs: list, today: dt.date) -> str:
    path = note_path(paths)
    try:
        with open(path, encoding="utf-8-sig") as f:
            existing = f.read()
    except OSError:
        existing = None
    text = render_note(paths, recs, today, existing)
    if text != existing:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    return path


def run(paths: Paths, modules: list | None = None, today: dt.date | None = None, say=print,
        learner_core=None) -> dict:
    """Every module (or `modules`): sidecars, the hub dataset, the vault
    note, the learner model, Don's notes. Returns {records, rows, touched}."""
    today = today or dt.date.today()
    every = conf.modules(paths)
    modules = every if modules is None else modules
    state = ingest.load_state(paths)
    recs, touched = [], []
    for module in modules:
        rec = build(paths, module, state, every, today)
        if not (rec["lectures"]["on_keats"] or rec["topics"] or rec["coursework"]):
            continue
        touched.append(don.save_json(paths, module_file(module), rec))
        rec["folder"] = module.folder
        recs.append(rec)
        say(_fmt(_SAY["study"], module=module.title, read=len(rec["lectures"]["read"]),
                 on_keats=len(rec["lectures"]["on_keats"]), weak=len(rec["weak"]),
                 next=rec["next"][0]["text"] if rec["next"] else "-"))
        stats = don.load_json(paths, CONFIG["don"]["anki_file"].format(slug=module.slug)) or {}
        if stats.get("topics"):
            touched += don.feed_learner(module, stats["topics"], today, say=say, core=learner_core)
    rows = [hub_row(r) for r in recs]
    if modules is not every:  # one module asked for: keep the others' rows
        names = {r["module"] for r in rows}
        rows = [r for r in (don.load_json(paths, _PR["dataset_file"]) or []) if r.get("module") not in names] + rows
        order = [m.title for m in every]
        rows.sort(key=lambda r: order.index(r["module"]) if r["module"] in order else len(order))
    touched.append(don.save_json(paths, _PR["dataset_file"], rows))
    all_recs = recs
    if modules is not every:
        known = {r["module"] for r in recs}
        for m in every:
            if m.title not in known:
                old = don.load_json(paths, module_file(m))
                if old:
                    all_recs.append({**old, "folder": m.folder})
        order = [m.title for m in every]
        all_recs.sort(key=lambda r: order.index(r["module"]) if r["module"] in order else len(order))
    path = write_note(paths, all_recs, today)
    touched.append(path)
    say(_fmt(_SAY["study_note"], path=path))
    touched += don.refresh(paths, state, every, today, say=say)
    return {"records": recs, "rows": rows, "touched": touched}


# -- readers for the study brief and the hub ---------------------------------------------

def _default_paths(vault: str | None = None) -> Paths:
    if vault is None:
        return conf.paths()
    return Paths(vault=vault, state=os.environ.get(CONFIG["paths"]["state_env"])
                 or os.path.join(conf.REPO, *CONFIG["paths"]["state"]))


def focus(module_text: str, vault: str | None = None) -> dict:
    """What the study brief needs for one module, from the last progress run:
    {tutor, weak: [{topic, weeks, coursework, notes}], tutor_notes: {week: stem}}.
    Empty when there is none. Only notes that exist are named."""
    paths = _default_paths(vault)
    module = conf.find_module(module_text, conf.modules(paths))
    if module is None:
        return {}
    rec = don.load_json(paths, module_file(module))
    if not rec:
        return {}
    weak = [{"topic": w["topic"], "weeks": w["weeks"], "coursework": w["coursework"],
             "notes": [(n, s) for n, s in _tutor_notes(module, w["weeks"])]} for w in rec.get("weak", [])]
    tutor_notes = {int(n): s for n, s in _tutor_notes(module, [int(n) for n, _s in rec.get("tutor_notes", [])])}
    return {"tutor": conf.tutor_name(), "weak": weak, "tutor_notes": tutor_notes}


def hub_rows(vault: str | None = None) -> list:
    """The study table for the Atrium hub, as the last progress run wrote it."""
    return don.load_json(_default_paths(vault), _PR["dataset_file"]) or []
