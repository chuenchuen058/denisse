"""Practice: quizzes from the cards, exam-style problems, and marking (study tutor 3/4).

    pick_quiz(paths, module, topic, n)   n cards, weak topics first (Anki stats), then due, then the rest
    set_exam(paths, module, topic, ..)   a longer problem in the style of the coursework and past papers
                                         in the module folder (tutor.json exam.paper_globs), with a mark
                                         scheme the tutor keeps to itself
    open_item(paths, module)             what he is answering right now (practice sidecar "open")
    mark_quiz / mark_exam                his answer against the card backs / the mark scheme, step by step
    record(...)                          the result into the history, the practice note, the progress note

One open item per module: setting a new question replaces an unanswered one.
Engines are cards.engines (local = Ollama, claude = delegate.py). Every
sentence and limit is tutor.json; nothing here names a model, a path or the tutor.
"""
from __future__ import annotations

import datetime as dt
import glob
import os
import random

from . import cards, don, extract, model, rules, sources, teach
from .conf import CONFIG, Module, Paths

_Q = CONFIG["quiz"]
_X = CONFIG["exam"]
_N = CONFIG["note"]
_SAY = CONFIG["say"]

KIND_QUIZ = "quiz"
KIND_EXAM = "exam"


# -- the practice sidecar ----------------------------------------------------------

def sidecar(module: Module) -> str:
    return _Q["practice_file"].format(slug=module.slug)


def load(paths: Paths, module: Module) -> dict:
    data = don.load_json(paths, sidecar(module)) or {}
    data.setdefault("module", module.title)
    data.setdefault("code", module.code)
    data.setdefault("open", None)
    data.setdefault("history", [])
    return data


def open_item(paths: Paths, module: Module) -> dict | None:
    return load(paths, module).get("open")


def note_path(paths: Paths, module: Module) -> str:
    return os.path.join(paths.don_dir, _X["note_file"].format(**don.ctx(module)))


# -- quiz --------------------------------------------------------------------------

def _stats(paths: Paths, module: Module) -> dict:
    return don.load_json(paths, CONFIG["don"]["anki_file"].format(slug=module.slug)) or {}


def pick_quiz(paths: Paths, module: Module, topic: str = "", n: int | None = None, rng=None) -> list:
    """Up to n cards: the order in tutor.json quiz.order (weak topics by the
    Anki stats, then due ones, then the rest), a random pick within each."""
    n = n or _Q["questions"]
    rng = rng or random.Random()
    weeks = rules.scope_weeks(paths, module)  # an exam ahead: the quiz stays inside its weeks
    pool = [c for c in cards.all_cards(cards.load(paths, module)) if rules.in_scope(c, weeks)]
    tw = teach._words(topic)
    if tw:
        pool = [c for c in pool if tw <= teach._words(c.get("topic", "")) or tw <= teach._words(c["front"])]
    stats = _stats(paths, module)
    weak = {t.lower() for t in stats.get("weak", [])}
    due = {t["topic"].lower() for t in stats.get("topics", []) if t.get("due")}
    buckets = {"weak": [], "due": [], "rest": []}
    for c in pool:
        key = (c.get("topic") or "").lower()
        buckets["weak" if key in weak else "due" if key in due else "rest"].append(c)
    out = []
    for name in _Q["order"]:
        chosen = list(buckets.get(name, []))
        rng.shuffle(chosen)
        out += chosen[:max(0, n - len(out))]
    return out[:n]


def set_quiz(paths: Paths, module: Module, picked: list, topic: str = "", today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    data = load(paths, module)
    data["open"] = {"kind": KIND_QUIZ, "date": today.isoformat(), "topic": topic,
                    "cards": [{"n": i, "id": c["id"], "front": c["front"], "back": c["back"],
                               "topic": c.get("topic", "")} for i, c in enumerate(picked, 1)]}
    don.save_json(paths, sidecar(module), data)
    return data["open"]


def mark_quiz(module: Module, item: dict, answer: str, calls: dict | None = None) -> dict:
    """{right, asked, results: [{n, front, back, correct|None, comment}]}."""
    c = don.ctx(module)
    items = "\n".join(_Q["mark_item"].format(**card) for card in item["cards"])
    system = _Q["mark_system"].format(**c) + rules.block("quiz")
    user = _Q["mark_user"].format(items=items, answer=answer, schema=_Q["mark_schema"], **c)
    data = cards._ask(cards.engine_call(_Q["mark_engine"], calls), system, user, _Q["mark_schema"])
    verdicts = {}
    for r in data.get("results") or []:
        if isinstance(r, dict) and str(r.get("n", "")).strip().isdigit():
            verdicts[int(r["n"])] = r
    results = []
    for card in item["cards"]:
        v = verdicts.get(card["n"])
        results.append({**card, "correct": (bool(v.get("correct")) if v else None),
                        "comment": teach._clip(v.get("comment"), _Q["comment_chars"]) if v else ""})
    return {"right": sum(1 for r in results if r["correct"]), "asked": len(results),
            "answered": sum(1 for r in results if r["correct"] is not None), "results": results}


# -- exam-style questions -------------------------------------------------------------

def style_text(module: Module) -> tuple:
    """(text, file names): the coursework and past papers in the module
    folder, read the way ingest reads the inbox, capped."""
    parts, names, seen = [], [], set()
    for pattern in _X["paper_globs"]:
        for path in sorted(glob.glob(os.path.join(glob.escape(module.folder), pattern))):
            if not os.path.isfile(path) or os.path.normcase(path) in seen:
                continue
            seen.add(os.path.normcase(path))
            if path.lower().endswith(CONFIG["sources"]["note_ext"]):
                _fm, text = sources.frontmatter(sources._read(path))
            else:
                text, _why = extract.read(path)
            text = (text or "").strip()
            if not text:
                continue
            names.append(os.path.splitext(os.path.basename(path))[0])
            parts.append(CONFIG["teach"]["material_heading"].format(name=names[-1]) + "\n" + text[:_X["style_per_file"]])
    return "\n\n".join(parts)[:_X["style_chars"]], names


def _normalise_exam(data: dict) -> dict:
    scheme = []
    for s in (data.get("mark_scheme") or [])[:_X["max_scheme"]]:
        if isinstance(s, dict) and teach._clip(s.get("step"), _X["comment_chars"]):
            try:
                marks = int(s.get("marks") or 0)
            except (TypeError, ValueError):
                marks = 0
            scheme.append({"step": teach._clip(s["step"], _X["comment_chars"]), "marks": marks})
    try:
        total = int(data.get("marks") or 0) or sum(s["marks"] for s in scheme) or _X["marks"]
    except (TypeError, ValueError):
        total = sum(s["marks"] for s in scheme) or _X["marks"]
    question = str(data.get("question") or "").strip()[:_X["question_chars"]]
    return {"title": teach._clip(data.get("title"), _X["title_chars"]), "question": question,
            "marks": total, "mark_scheme": scheme}


def set_exam(paths: Paths, module: Module, topic: str = "", calls: dict | None = None,
             num_ctx: int | None = None, today: dt.date | None = None) -> dict:
    """Ask the engine for a problem and keep it as the open item. Raises
    model.ModelError when it never gives a usable question."""
    today = today or dt.date.today()
    c = don.ctx(module)
    style, names = style_text(module)
    topic = topic or rules.weakest(paths, module, today)  # no topic asked: what Anki says he keeps losing
    if topic:
        found = teach.material(paths, module, topic, num_ctx)
        material = found["text"]
    else:
        found = {"text": "", "links": []}
        material = ""
        # No topic: the latest tutor note, so the problem is on what he is doing now.
        weeks = rules.scope_weeks(paths, module, today)
        tutor_notes = [s for s in sources.collect(paths, module) if s.kind == CONFIG["sources"]["tutor"] and s.week
                       and (weeks is None or s.week in weeks)]
        if tutor_notes:
            latest = max(tutor_notes, key=lambda s: s.week)
            material = latest.text[:CONFIG["teach"]["note_chars"]]
            found["links"] = [latest.link]
    limit = int(model.chunk_chars(num_ctx) * CONFIG["teach"]["material_share"])
    system = _X["system"].format(**c) + rules.block("exam")
    user = _X["user"].format(topic=_X["topic_line"].format(topic=topic) if topic else "",
                             style=style or _X["style_none"], marks=_X["marks"], schema=_X["schema"],
                             material=material[:limit], **c)
    item = _normalise_exam(cards._ask(cards.engine_call(_X["engine"], calls), system, user, _X["schema"]))
    if not item["question"]:
        raise model.ModelError(_SAY["cards_no_json"])
    item.update(kind=KIND_EXAM, date=today.isoformat(), topic=topic, style=names, links=found["links"])
    data = load(paths, module)
    data["open"] = item
    don.save_json(paths, sidecar(module), data)
    return item


def mark_prompt(module: Module, item: dict, answer: str) -> tuple:
    c = don.ctx(module)
    scheme = "\n".join(_X["mark_scheme_line"].format(**s) for s in item["mark_scheme"])
    system = _X["mark_system"].format(**c) + rules.block("exam")
    user = _X["mark_user"].format(title=item["title"], total=item["marks"], question=item["question"],
                                  scheme=scheme, answer=answer, schema=_X["mark_schema"], **c)
    return system, user


def mark_exam(module: Module, item: dict, answer: str, calls: dict | None = None, engine: str | None = None) -> dict:
    """{marks, total, steps: [{step, awarded, of, comment}], feedback}."""
    system, user = mark_prompt(module, item, answer)
    data = cards._ask(cards.engine_call(engine or _X["marking_engine"], calls), system, user, _X["mark_schema"])
    steps = []
    for s in (data.get("steps") or [])[:_X["max_scheme"]]:
        if not isinstance(s, dict):
            continue
        try:
            awarded, of = int(s.get("awarded") or 0), int(s.get("of") or 0)
        except (TypeError, ValueError):
            awarded, of = 0, 0
        steps.append({"step": teach._clip(s.get("step"), _X["comment_chars"]), "awarded": awarded, "of": of,
                      "comment": teach._clip(s.get("comment"), _X["comment_chars"])})
    try:
        marks = int(data.get("marks") if data.get("marks") is not None else sum(s["awarded"] for s in steps))
        total = int(data.get("total") or item["marks"])
    except (TypeError, ValueError):
        marks, total = sum(s["awarded"] for s in steps), item["marks"]
    return {"marks": max(0, min(marks, total)), "total": total, "steps": steps,
            "feedback": teach._clip(data.get("feedback"), _X["feedback_chars"])}


# -- recording -----------------------------------------------------------------------

def render_note(paths: Paths, module: Module, data: dict, today: dt.date) -> str:
    c = don.ctx(module, today=today.isoformat(), name=_X["note_name"].format(**don.ctx(module)))
    lines = [line.format(links=don._links(paths, module, note_path(paths, module)), **c) for line in _X["note_front"]]
    item = data.get("open")
    exams = [h for h in data["history"] if h.get("kind") == KIND_EXAM]
    if item and item.get("kind") == KIND_EXAM:
        lines += [_X["entry_open"].format(title=item["title"], marks=item["marks"], date=item["date"]), "",
                  item["question"], "", _X["entry_open_line"].format(**c), ""]
    for h in reversed(exams):
        lines += [_X["entry_heading"].format(date=h["date"], title=h["title"], marks=h["marks"], total=h["total"]), "",
                  _X["entry_question"].format(question=h["question"]), ""]
        lines += [_X["entry_step"].format(**s) for s in h["steps"]]
        if h.get("feedback"):
            lines += ["", _X["entry_feedback"].format(feedback=h["feedback"])]
        lines.append("")
    if not exams and not (item and item.get("kind") == KIND_EXAM):
        lines += [_X["none"].format(**c), ""]
    quizzes = [h for h in data["history"] if h.get("kind") == KIND_QUIZ]
    if quizzes:
        lines += [_X["quizzes_heading"], ""]
        lines += [_X["quiz_line"].format(date=h["date"], right=h["right"], asked=h["asked"],
                                         answered=h.get("answered", h["asked"]),
                                         topic=_X["quiz_line_topic"].format(topic=h["topic"]) if h.get("topic") else "")
                  for h in reversed(quizzes)]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def record(paths: Paths, module: Module, entry: dict, today: dt.date | None = None, say=print) -> dict:
    """The marked item into the history (the open slot cleared), the
    practice note and the progress notes. Returns {touched, note}."""
    today = today or dt.date.today()
    data = load(paths, module)
    data["open"] = None
    data["history"] = (data["history"] + [{"date": today.isoformat(), **entry}])[-_Q["history_keep"]:]
    data["updated"] = today.isoformat()
    return _write(paths, module, data, today, say)


def write_open(paths: Paths, module: Module, today: dt.date | None = None, say=print) -> dict:
    """Re-render the practice note after a question is set (the open one is shown)."""
    today = today or dt.date.today()
    return _write(paths, module, load(paths, module), today, say)


def _write(paths: Paths, module: Module, data: dict, today: dt.date, say) -> dict:
    touched = [don.save_json(paths, sidecar(module), data)]
    from . import ingest
    state = ingest.load_state(paths)
    note = don.write_note(note_path(paths, module), render_note(paths, module, data, today), state, say)
    if note:
        say(_SAY["practice"].format(path=note))
        touched.append(note)
    touched += don.refresh(paths, state, today=today, say=say)
    ingest.save_state(paths, state)
    touched.append(paths.state_file())
    return {"touched": touched, "note": note}
