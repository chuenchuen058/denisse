"""The tutor as a specialist consultant (study tutor 5/5).

Chuen (5 Nov 2026): "integrate it with the other AI models and treat Don
like a professional opinion on task". Any agent working on something about
his modules, coursework, revision or learning asks the tutor first and
weighs the answer the way it would a subject expert's: informed and grounded
in his own notes, advisory, never an override.

    consult(paths, question, module_text="")   the structured opinion:
        answer      what the notes say that bears on the question
        confidence  low / medium / high, from how well the notes cover it
        sources     the vault notes the answer was given from, as links
        caveats     what the notes don't cover, and anything that went wrong
        who         the engine that answered (local model or Claude), or none
    record(paths, result)                        into the Don area: sidecar + note
    render(result)                               the CLI's text form

Nothing is invented: the sources are the notes the code chose and gave to
the model, never names the model produced; no notes means no model call and
a low-confidence answer that says so. Every sentence, limit and engine name
is tutor.json "consult"; nothing here names a path, a model or the tutor.
"""
from __future__ import annotations

import datetime as dt
import os

from . import cards, conf, don, model, sources, teach, topics
from .conf import CONFIG, Module, Paths

_K = CONFIG["consult"]
_N = CONFIG["note"]
_SAY = CONFIG["say"]


def _words(text: str) -> set:
    return topics._words(text)


def _clip(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


# -- which notes --------------------------------------------------------------

def gather(paths: Paths, module: Module, question: str, num_ctx: int | None = None) -> dict:
    """{text, links, notes, coverage, picked}: the module's notes that hold
    the question's words, best first, capped; coverage is the share of the
    question's content words some chosen note holds."""
    qw = _words(question)
    out = {"text": "", "links": [], "notes": 0, "coverage": 0.0, "picked": []}
    if not qw:
        return out
    boost_weeks = set()
    for t in (topics.load(paths, module) or {}).get("topics", []):
        if _words(t.get("name", "")) & qw:
            boost_weeks.update(int(w) for w in t.get("lectures", []))
    tutor_kind = CONFIG["sources"]["tutor"]
    scored = []
    for src in sources.collect(paths, module):
        overlap = qw & _words(src.text)
        if len(overlap) < _K["min_overlap_words"]:
            continue
        score = len(overlap) + (_K["week_boost"] if src.kind == tutor_kind and src.week in boost_weeks else 0)
        scored.append((-score, src.kind != tutor_kind, src.rel, src, overlap))
    scored.sort(key=lambda s: s[:3])
    picked = scored[:_K["max_notes"]]
    if not picked:
        return out
    covered = set()
    parts = []
    for _score, _own, _rel, src, overlap in picked:
        covered |= overlap
        parts.append(_K["material_heading"].format(name=src.stem) + "\n" + src.text[:_K["note_chars"]])
        out["links"].append(src.link)
    limit = int(model.chunk_chars(num_ctx) * _K["material_share"])
    out["text"] = "\n\n".join(parts)[:limit]
    out["notes"] = len(picked)
    out["coverage"] = len(covered) / len(qw)
    out["picked"] = [src for _s, _o, _r, src, _ov in picked]
    return out


# -- confidence --------------------------------------------------------------

def grade(coverage: float, notes: int, confident: bool = True) -> str:
    """The confidence label (consult.confidence): no notes is low; otherwise
    by coverage and note count, dropped a level when the model itself was
    not confident."""
    c = _K["confidence"]
    order = c["order"]
    if notes <= 0:
        level = order[0]
    elif coverage >= c["high_share"] and notes >= c["high_min_notes"]:
        level = order[-1]
    elif coverage >= c["medium_share"]:
        level = order[len(order) // 2]
    else:
        level = order[0]
    if not confident and c["downgrade_when_not_confident"]:
        level = order[max(0, order.index(level) - 1)]
    return c["labels"][level]


# -- the model ----------------------------------------------------------------

def prompt(module: Module, question: str, found: dict) -> tuple:
    c = don.ctx(module)
    return (_K["system"].format(**c),
            _K["user"].format(question=question, schema=_K["schema"], text=found["text"], **c))


def normalise(data: dict) -> dict:
    caveats = data.get("caveats") or []
    if isinstance(caveats, str):
        caveats = [caveats]
    return {"answer": _clip(data.get("answer"), _K["answer_chars"]),
            "caveats": [_clip(x, _K["caveat_chars"]) for x in caveats[:_K["max_caveats"]] if _clip(x, _K["caveat_chars"])],
            "confident": bool(data.get("confident", True))}


def ask(engine: str, system: str, user: str, calls: dict | None = None) -> dict:
    """One engine's opinion, normalised. Raises model.ModelError when it never gives JSON."""
    return normalise(cards._ask(cards.engine_call(engine, calls), system, user))


def _hand_offs_off(engine: str, calls: dict | None) -> bool:
    """A delegate engine with no injected call is off when delegate.DRY_RUN is set."""
    if calls and engine in calls:
        return False
    if CONFIG["cards"]["engines"].get(engine, {}).get("via") != "delegate":
        return False
    try:
        import delegate
        return bool(getattr(delegate, "DRY_RUN", False))
    except Exception:  # noqa: BLE001 -- no delegate module: no hand-off either
        return True


def _who(engine: str | None) -> str:
    return engine or _K["who_none"]


def who_label(who: str) -> str:
    return _K["who_labels"].get(who, who)


# -- the consult ----------------------------------------------------------------

def _resolve(paths: Paths, question: str, module_text: str) -> tuple:
    """(module or None, every module, the line to say when the named module is unknown)."""
    modules = conf.modules(paths)
    if module_text.strip():
        module = conf.find_module(module_text, modules)
        if module is None:
            return None, modules, _K["no_module"].format(text=module_text.strip(),
                                                         modules=_N["list_sep"].join(m.title for m in modules))
        return module, modules, None
    return conf.find_module(question, modules), modules, None


def consult(paths: Paths, question: str, module_text: str = "", calls: dict | None = None,
            today: dt.date | None = None, num_ctx: int | None = None) -> dict:
    """The opinion. Never raises for a model that fails: that becomes a
    caveat and, with no answer at all, `who` is none and `answer` empty."""
    today = today or dt.date.today()
    question = _clip(question, 10 ** 6)
    c = don.ctx()
    out = {"question": question, "date": today.isoformat(), "answer": "", "confidence": grade(0, 0),
           "sources": [], "caveats": [], "who": _K["who_none"], "module": "", "code": "", "notes": 0,
           "coverage": 0.0}
    module, modules, problem = _resolve(paths, question, module_text or "")
    if problem:
        out["caveats"].append(problem)
        return out
    found = None
    if module is not None:
        found = gather(paths, module, question, num_ctx)
    else:
        for m in modules:
            f = gather(paths, m, question, num_ctx)
            if f["notes"] and (found is None or (f["coverage"], f["notes"]) > (found["coverage"], found["notes"])):
                module, found = m, f
    if module is None or not found or not found["notes"]:
        out["answer"] = _K["no_notes"].format(
            module=_K["no_notes_module"].format(module=module.title) if module else "", **c)
        out["caveats"].append(_K["caveat_no_notes"])
        if module is not None:
            out.update(module=module.title, code=module.code)
        return out
    out.update(module=module.title, code=module.code, notes=found["notes"],
               coverage=round(found["coverage"], 2), sources=list(found["links"]))
    system, user = prompt(module, question, found)
    engines = _K["engines"]
    order = [engines["hard"]] if teach.is_hard(question) else [engines["local"], engines["fallback"]]
    answer, who, caveats = None, None, []
    try:
        answer, who = ask(order[0], system, user, calls), order[0]
    except model.ModelError as exc:
        caveats.append(_K["caveat_engine_failed"].format(engine=who_label(order[0]), why=exc))
    if len(order) > 1 and (answer is None or not answer["answer"] or not answer["confident"]):
        fallback = order[1]
        if _hand_offs_off(fallback, calls):
            caveats.append(_K["caveat_fallback_off"].format(engine=who_label(fallback)))
        else:
            try:
                answer, who = ask(fallback, system, user, calls), fallback
            except model.ModelError as exc:
                caveats.append(_K["caveat_fallback_failed"].format(engine=who_label(fallback), why=exc))
    if answer is None or not answer["answer"]:
        out["caveats"] = caveats
        return out
    if not answer["confident"]:
        caveats.append(_K["caveat_not_confident"])
    out.update(answer=answer["answer"], who=_who(who), caveats=caveats + answer["caveats"],
               confidence=grade(found["coverage"], found["notes"], answer["confident"]))
    return out


def answered(result: dict) -> bool:
    """An opinion was given from the notes (as against: no notes, no model, no module)."""
    return bool(result.get("answer")) and result.get("who", _K["who_none"]) != _K["who_none"]


# -- recording -------------------------------------------------------------------

def load(paths: Paths) -> dict:
    data = don.load_json(paths, _K["sidecar"]) or {}
    data.setdefault("consults", [])
    return data


def note_path(paths: Paths) -> str:
    return os.path.join(paths.don_dir, _K["note_file"].format(**don.ctx()))


def render_note(paths: Paths, data: dict, today: dt.date) -> str:
    c = don.ctx(today=today.isoformat(), name=_K["note_name"].format(**don.ctx()), hub=don.hub_link(paths))
    lines = [line.format(**c) for line in _K["note_front"]]
    if not data["consults"]:
        lines.append(_K["none"])
    for item in reversed(data["consults"]):
        lines += [_K["entry_heading"].format(date=item["date"], question=_clip(item["question"], _K["question_chars"])),
                  "", _K["entry_meta"].format(module=item.get("module") or "-", confidence=item["confidence"],
                                              who=who_label(item["who"])), "",
                  _K["entry_answer"].format(answer=item["answer"])]
        if item.get("caveats"):
            lines += ["", _K["entry_caveats_heading"]] + [_K["entry_caveat"].format(text=x) for x in item["caveats"]]
        if item.get("sources"):
            lines += ["", _K["entry_sources"].format(links=_N["list_sep"].join(item["sources"]))]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def record(paths: Paths, result: dict, today: dt.date | None = None, say=print) -> dict:
    """Keep the consult (sidecar + note), refresh the hub, and say which
    files were touched. Returns {touched, note}."""
    today = today or dt.date.fromisoformat(result["date"])
    data = load(paths)
    data["consults"] = (data["consults"] + [{k: result[k] for k in
                                             ("date", "question", "module", "code", "answer", "confidence",
                                              "sources", "caveats", "who", "notes", "coverage")}])[-_K["keep"]:]
    data["updated"] = today.isoformat()
    touched = [don.save_json(paths, _K["sidecar"], data)]
    from . import ingest
    state = ingest.load_state(paths)
    note = don.write_note(note_path(paths), render_note(paths, data, today), state, say)
    if note:
        say(_SAY["consult"].format(path=note))
        touched.append(note)
    touched += don.refresh(paths, state, modules=[], today=today, say=say)
    ingest.save_state(paths, state)
    touched.append(paths.state_file())
    return {"touched": touched, "note": note}


# -- text ---------------------------------------------------------------------------

def render(result: dict) -> str:
    """The opinion as the command line prints it."""
    return _K["text"].format(confidence=result["confidence"], who=who_label(result["who"]), answer=result["answer"],
                             sources=_N["list_sep"].join(result["sources"]) or _K["text_no_sources"],
                             caveats=_N["list_sep"].join(result["caveats"]) or _K["text_no_caveats"], **don.ctx())
