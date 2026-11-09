"""Explain a topic from his own material (study tutor 3/4).

    material(paths, module, topic)   what the vault says about the topic: the
                                     topic map entry, the tutor notes of the
                                     weeks it names, his own notes naming it
    explain(paths, module, topic)    the lesson: local model first; the hard
                                     topics (tutor.json teach.hard_topics) and a
                                     local answer that fails its self-check go
                                     to the fallback engine (Claude through
                                     delegate.py, never an API key)
    record(paths, module, lesson)    the lesson into the Don area: a JSON
                                     sidecar and a readable lessons note

The learner model (learner/) is read, never written, for how he likes to be
taught: the beliefs in teach.learner_areas go into the prompt. Every
sentence and limit is tutor.json; nothing here names a model, a path or the
tutor.
"""
from __future__ import annotations

import datetime as dt
import os

from . import cards, conf, don, model, notes, rules, sources, topics
from .conf import CONFIG, Module, Paths

_T = CONFIG["teach"]
_N = CONFIG["note"]
_D = CONFIG["don"]
_SAY = CONFIG["say"]


class Hard(Exception):
    """The topic is marked hard: the local model is not asked."""


def _words(text: str) -> set:
    return topics._words(text)


def _mentions(text: str, want: set) -> bool:
    return bool(want) and want <= _words(text)


def is_hard(topic: str) -> bool:
    """Marked hard in tutor.json: every word of a hard_topics entry is in the topic (or the reverse)."""
    tw = _words(topic)
    for hard in _T["hard_topics"]:
        hw = _words(hard)
        if hw and (hw <= tw or tw <= hw):
            return True
    return False


def learner_lines(core=None) -> str:
    """What the learner model says about how he learns, as prompt lines."""
    try:
        if core is None:
            from learner import core
        data = core.load()
    except Exception:  # noqa: BLE001 -- no learner store is not a reason not to teach
        return _T["learner_none"]
    areas = set(_T["learner_areas"])
    lines = [_T["learner_line"].format(claim=b["claim"]) for b in data.get("beliefs", [])
             if b.get("status") == "current" and b.get("area") in areas]
    return "\n".join(lines[:_T["learner_max"]]) or _T["learner_none"]


# -- material ------------------------------------------------------------------

def _topic_entry(paths: Paths, module: Module, topic: str) -> dict | None:
    tm = topics.load(paths, module) or {}
    tw = _words(topic)
    best = None
    for t in tm.get("topics", []):
        nw = _words(t["name"])
        if tw and nw and (tw <= nw or nw <= tw):
            if best is None or len(nw & tw) > len(_words(best["name"]) & tw):
                best = t
    return best


def material(paths: Paths, module: Module, topic: str, num_ctx: int | None = None) -> dict:
    """{text, links, weeks, entry}: what the vault holds on the topic, in
    the order the model should read it, capped to the chunk share."""
    limit = int(model.chunk_chars(num_ctx) * _T["material_share"])
    entry = _topic_entry(paths, module, topic)
    weeks = list(entry["lectures"]) if entry else []
    parts, links, seen = [], [], set()
    tw = _words(topic)

    def add(name: str, rel: str, text: str, cap: int):
        if rel in seen or not text.strip():
            return
        seen.add(rel)
        parts.append(_T["material_heading"].format(name=name) + "\n" + text.strip()[:cap])
        links.append(CONFIG["sources"]["source_link"].format(rel=os.path.splitext(rel)[0], stem=name))

    # The tutor notes of the weeks the topic map names, whole.
    for w in weeks:
        path = notes.note_path(module, w)
        if os.path.isfile(path):
            _fm, body = sources.frontmatter(sources._read(path))
            add(os.path.splitext(os.path.basename(path))[0], notes._rel(paths.vault, path), body, _T["note_chars"])
    # Then every other source note that names the topic: tutor notes first, then his own.
    own = 0
    for src in sources.collect(paths, module):
        if src.rel in seen or not _mentions(src.text, tw):
            continue
        if src.kind == CONFIG["sources"]["tutor"]:
            add(src.stem, src.rel, src.text, _T["note_chars"])
        elif own < _T["own_notes_max"]:
            own += 1
            add(src.stem, src.rel, src.text, _T["own_note_chars"])
    text = "\n\n".join(parts)
    if len(text) > limit:
        text = text[:limit]
    return {"text": text, "links": links, "weeks": weeks, "entry": entry}


# -- the lesson ----------------------------------------------------------------

def _clip(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def normalise(data: dict) -> dict:
    steps = data.get("steps") or []
    if isinstance(steps, str):
        steps = [steps]
    return {"summary": _clip(data.get("summary"), _T["summary_chars"]),
            "steps": [_clip(s, _T["step_chars"]) for s in steps[:_T["max_steps"]] if _clip(s, _T["step_chars"])],
            "question": _clip(data.get("question"), _T["question_chars"]),
            "answer": _clip(data.get("answer"), _T["answer_chars"]),
            "confident": bool(data.get("confident", True))}


def self_check(lesson: dict) -> bool:
    """A local answer stands when it has enough real steps, a question, and
    the model itself was confident (teach.self_check)."""
    c = _T["self_check"]
    steps = [s for s in lesson["steps"] if len(s) >= c["min_step_chars"]]
    if len(steps) < c["min_steps"] or not lesson["summary"]:
        return False
    if c["needs_question"] and not lesson["question"]:
        return False
    if c["needs_confident"] and not lesson["confident"]:
        return False
    return True


def prompt(module: Module, topic: str, found: dict, core=None) -> tuple:
    c = don.ctx(module)
    system = _T["system"].format(**c) + rules.block("explain")
    weeks = _T["weeks"].format(weeks=_N["list_sep"].join(map(str, found["weeks"]))) if found["weeks"] else ""
    user = _T["user"].format(topic=topic, weeks=weeks, learner=learner_lines(core), schema=_T["schema"],
                             text=found["text"], **c)
    return system, user


def ask(engine: str, system: str, user: str, calls: dict | None = None) -> dict:
    """One engine's lesson, normalised. Raises model.ModelError when it never gives JSON."""
    return normalise(cards._ask(cards.engine_call(engine, calls), system, user, _T["schema"]))


def explain(paths: Paths, module: Module, topic: str, calls: dict | None = None, core=None,
            num_ctx: int | None = None) -> dict:
    """The local lesson, or the reason it must go further.

    Returns {material, lesson, engine, escalate, why}: `lesson` is None when
    there is no material (nothing to teach from) or the topic is hard
    (teach.engines.hard does it instead); `escalate` names the engine that
    should take over and `why` says why (hard, check)."""
    found = material(paths, module, topic, num_ctx)
    out = {"material": found, "lesson": None, "engine": None, "escalate": None, "why": None}
    if not found["text"]:
        return out
    if is_hard(topic):
        out.update(escalate=_T["engines"]["hard"], why="hard")
        return out
    system, user = prompt(module, topic, found, core)
    lesson = ask(_T["engines"]["local"], system, user, calls)
    out.update(lesson=lesson, engine=_T["engines"]["local"])
    if not self_check(lesson):
        out.update(escalate=_T["engines"]["fallback"], why="check")
    return out


def explain_with(paths: Paths, module: Module, topic: str, engine: str, calls: dict | None = None,
                 core=None, num_ctx: int | None = None, found: dict | None = None) -> dict:
    """The lesson from one named engine (the fallback path)."""
    found = found or material(paths, module, topic, num_ctx)
    system, user = prompt(module, topic, found, core)
    return ask(engine, system, user, calls)


# -- recording -------------------------------------------------------------------

def sidecar(module: Module) -> str:
    return _T["lesson_file"].format(slug=module.slug)


def load(paths: Paths, module: Module) -> dict:
    data = don.load_json(paths, sidecar(module)) or {}
    data.setdefault("module", module.title)
    data.setdefault("code", module.code)
    data.setdefault("lessons", [])
    return data


def lessons_path(paths: Paths, module: Module) -> str:
    return os.path.join(paths.don_dir, _T["lessons_note_file"].format(**don.ctx(module)))


def render_note(paths: Paths, module: Module, data: dict, today: dt.date) -> str:
    c = don.ctx(module, today=today.isoformat(), name=_T["lessons_name"].format(**don.ctx(module)))
    lines = [line.format(links=don._links(paths, module, lessons_path(paths, module)), **c)
             for line in _T["lessons_front"]]
    if not data["lessons"]:
        lines.append(_T["lessons_none"].format(**c))
    for lesson in reversed(data["lessons"]):
        lines += [_T["lesson_heading"].format(date=lesson["date"], topic=lesson["topic"], engine=lesson["engine"]), "",
                  _T["lesson_summary"].format(summary=lesson["summary"]), ""]
        lines += [_T["lesson_step"].format(n=i, text=s) for i, s in enumerate(lesson["steps"], 1)]
        if lesson.get("question"):
            lines += ["", _T["lesson_question"].format(question=lesson["question"]),
                      _T["lesson_answer"].format(answer=lesson["answer"])]
        if lesson.get("links"):
            lines += ["", _T["lesson_sources"].format(links=_N["list_sep"].join(lesson["links"]))]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def record(paths: Paths, module: Module, topic: str, lesson: dict, engine: str, links: list,
           today: dt.date | None = None, say=print) -> dict:
    """Keep the lesson (sidecar + note), refresh the progress notes, and say
    which files were touched. Returns {touched, note}."""
    today = today or dt.date.today()
    data = load(paths, module)
    data["lessons"] = (data["lessons"] + [{"date": today.isoformat(), "topic": topic, "engine": engine,
                                          "links": links, **lesson}])[-_T["lessons_keep"]:]
    data["updated"] = today.isoformat()
    touched = [don.save_json(paths, sidecar(module), data)]
    from . import ingest
    state = ingest.load_state(paths)
    note = don.write_note(lessons_path(paths, module), render_note(paths, module, data, today), state, say)
    if note:
        say(_SAY["lesson"].format(path=note))
        touched.append(note)
    touched += don.refresh(paths, state, today=today, say=say)
    ingest.save_state(paths, state)
    touched.append(paths.state_file())
    return {"touched": touched, "note": note}
