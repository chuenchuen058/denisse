"""The tutor as Denisse's chat tools (study tutor 3/4): tools.py registers
these, brain.json describes them, tool_policy.json says what each may do.

    explain(topic, module)        teach a topic from his notes; hard ones go to Claude
    quiz(module, topic, count)    a short check from the cards, weak topics first
    weak_topics(module)           what Anki (and the learner model) say he keeps losing
    exam_question(module, topic)  a longer problem in the coursework / past-paper style
    mark_answer(answer, module)   mark what he answered: quiz against the cards, exam
                                  against the mark scheme (Claude)
    round_offer(round_no)         study_mode's end-of-round line: offer the quiz

Every result starts with the tutor introducing itself (tutor.json chat.intro),
so the model's reply can carry the name. A Claude job runs in the background
(delegate.py's on_done shows the result when it lands) so the chat never
waits a minute on a tool; BACKGROUND=False (tests) runs it inline. PATHS,
CALLS and CORE are the test seams for the vault, the models and the learner.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading

from . import conf, consult as consult_, don, exam, model, rules, teach
from .conf import CONFIG, Module, Paths

_C = CONFIG["chat"]
_T = CONFIG["teach"]
_N = CONFIG["note"]
log = logging.getLogger("denisse.tutor")

PATHS: Paths | None = None   # tests and the eval: a Paths instead of the real vault
CALLS: dict | None = None    # tests: engine name -> call(system, user)
CORE = None                  # tests: a learner core
BACKGROUND = True            # Claude jobs in a thread (False: inline, for tests)
_jobs: list = []             # the threads started, so a test can join them


def _paths() -> Paths:
    """The vault, with its rules note read (rules.load: again only when it changed)."""
    paths = PATHS if PATHS is not None else conf.paths()
    try:
        rules.load(paths)
    except Exception as exc:  # noqa: BLE001 -- no rules is the defaults, never a failed tool
        _say(str(exc))
    return paths


def _say(line: str) -> None:
    log.info("%s", line)


def _ctx(module: Module | None = None, **more) -> dict:
    c = don.ctx(module, **more)
    c.setdefault("intro", _C["intro"].format(**c))
    return c


def _module_list(modules: list) -> str:
    return _N["list_sep"].join(m.title for m in modules)


def _note_link(path: str | None) -> str:
    return os.path.splitext(os.path.basename(path))[0] if path else ""


def _resolve(paths: Paths, text: str, topic: str = "") -> tuple:
    """(module, None) or (None, the line to say). With no module named, a
    topic that only one module's topic map or cards know picks that module;
    one module with any data at all is taken; otherwise ask which."""
    modules = conf.modules(paths)
    if not modules:
        return None, _C["no_modules"].format(**_ctx())
    if text and text.strip():
        module = conf.find_module(text, modules)
        if module is None:
            return None, _C["no_module"].format(text=text.strip(), modules=_module_list(modules), **_ctx())
        return module, None
    hits = []
    tw = teach._words(topic)
    for m in modules:
        from . import topics as _topics, cards as _cards
        names = [t["name"] for t in (_topics.load(paths, m) or {}).get("topics", [])]
        names += [c.get("topic", "") for c in _cards.all_cards(_cards.load(paths, m))]
        if tw and any(tw <= teach._words(n) or (teach._words(n) and teach._words(n) <= tw) for n in names):
            hits.append(m)
    if len(hits) == 1:
        return hits[0], None
    from . import ingest
    state = ingest.load_state(paths)
    with_data = [m for m in modules if don.has_data(paths, m, state)]
    if len(with_data) == 1 and not hits:
        return with_data[0], None
    return None, _C["which_module"].format(modules=_module_list(hits or modules), **_ctx())


def _steps(lesson: dict) -> str:
    return "\n".join(_C["explain_step"].format(n=i, text=s) for i, s in enumerate(lesson["steps"], 1))


def _says(found: dict, lesson: dict) -> str:
    """The tutor's confidence in a lesson (part 5): every note the material
    names covers the topic, so the grade is by note count and the model's
    own confidence, said so Chuen can tell the tutor's view from Denisse's."""
    n = len(found.get("links", []))
    return _C["says"].format(confidence=consult_.grade(1.0 if n else 0.0, n, lesson.get("confident", True)),
                             n=n, **_ctx())


def _commit(paths: Paths, touched: list, what: str, module: Module) -> None:
    try:
        don.commit(paths, touched, what, module, say=_say)
    except Exception as exc:  # noqa: BLE001 -- a commit failure is logged, never shown as a lesson failure
        _say(_C["not_committed"].format(why=exc))


def _hand_off(work) -> None:
    """Run `work` (which returns the message to show) in the background and
    hand the message to delegate.on_done, the tray's hook for a finished
    Claude job. Inline when BACKGROUND is off."""
    def run():
        try:
            msg = work()
        except Exception as exc:  # noqa: BLE001 -- the thread must report, not die quietly
            msg = str(exc)
        try:
            import delegate
            if delegate.on_done:
                delegate.on_done(msg)
        except Exception:  # noqa: BLE001
            pass
    if not BACKGROUND:
        run()
        return
    t = threading.Thread(target=run, daemon=True)
    _jobs.append(t)
    t.start()


def _dry_run() -> bool:
    try:
        import delegate
        return bool(getattr(delegate, "DRY_RUN", False))
    except Exception:  # noqa: BLE001 -- no delegate module means no hand-off either way
        return True


# -- explain ---------------------------------------------------------------------------

def explain(topic: str, module: str = "") -> str:
    paths = _paths()
    today = dt.date.today()
    topic = " ".join((topic or "").split())
    mod, line = _resolve(paths, module, topic)
    if mod is None:
        return line
    topic = topic or rules.weakest(paths, mod, today)  # no topic asked: the weakest one in Anki
    c = _ctx(mod, topic=topic)
    try:
        out = teach.explain(paths, mod, topic, calls=CALLS, core=CORE)
    except model.ModelError as exc:
        return _C["model_failed"].format(what=_C["what_explanation"], why=exc, **c)
    found = out["material"]
    if not found["text"]:
        return _C["no_material"].format(**c)
    note_name = _note_link(teach.lessons_path(paths, mod))
    if out["lesson"] is not None and not out["escalate"]:
        rec = teach.record(paths, mod, topic, out["lesson"], out["engine"], found["links"], today, say=_say)
        _commit(paths, rec["touched"], "teach", mod)
        return _C["explain"].format(source=_C["from_local"], summary=out["lesson"]["summary"],
                                    steps=_steps(out["lesson"]), question=out["lesson"]["question"],
                                    answer=out["lesson"]["answer"], note=_note_link(rec["note"]) or note_name,
                                    says=_says(found, out["lesson"]), **c)
    # Hard, or the local answer failed its check: Claude, in the background.
    why = _C["why_hard" if out["why"] == "hard" else "why_check"].format(**c)
    if _dry_run():
        why = _C["why_dry"].format(**c)
        reply = _C["escalated"].format(why=why, note=note_name, **c)
    else:
        engine, lesson_local = out["escalate"], out["lesson"]

        def work():
            try:
                lesson = teach.explain_with(paths, mod, topic, engine, calls=CALLS, core=CORE, found=found)
            except model.ModelError as exc:
                teach.learn_from(paths, mod, topic, out, None, engine)  # still a failure worth counting
                return _C["claude_failed"].format(why=exc, **c)
            teach.learn_from(paths, mod, topic, out, lesson, engine)
            rec = teach.record(paths, mod, topic, lesson, engine, found["links"], today, say=_say)
            _commit(paths, rec["touched"], "teach", mod)
            return _C["claude_done"].format(summary=lesson["summary"], steps=_steps(lesson),
                                            question=lesson["question"], note=_note_link(rec["note"]) or note_name,
                                            says=_says(found, lesson), **c)
        _hand_off(work)
        reply = _C["escalated"].format(why=why, note=note_name, **c)
        if lesson_local is not None and lesson_local["steps"]:
            reply += _C["escalated_meanwhile"].format(summary=lesson_local["summary"], steps=_steps(lesson_local))
    return reply


# -- consult (part 5) -------------------------------------------------------------------

def consult(question: str, module: str = "") -> str:
    """The tutor's opinion on a study question, as the tutor's: answer with
    its confidence, the notes it came from and what they don't cover."""
    paths = _paths()
    question = " ".join((question or "").split())
    c = _ctx()
    if not question:
        return _C["consult_empty"].format(**c)
    result = consult_.consult(paths, question, module, calls=CALLS, today=dt.date.today())
    if consult_.answered(result):
        try:
            rec = consult_.record(paths, result, say=_say)
            _commit(paths, rec["touched"], "consult", None)
        except Exception as exc:  # noqa: BLE001 -- the opinion stands even when the vault write doesn't
            _say(_C["not_committed"].format(why=exc))
    return _C["consult"].format(confidence=result["confidence"], who=consult_.who_label(result["who"]),
                                answer=result["answer"], sources=_N["list_sep"].join(result["sources"]) or
                                CONFIG["consult"]["text_no_sources"],
                                caveats=_N["list_sep"].join(result["caveats"]) or CONFIG["consult"]["text_no_caveats"],
                                **c)


# -- quiz ------------------------------------------------------------------------------

def quiz(module: str = "", topic: str = "", count: int = 0) -> str:
    paths = _paths()
    topic = " ".join((topic or "").split())
    mod, line = _resolve(paths, module, topic)
    if mod is None:
        return line
    c = _ctx(mod)
    try:
        n = int(count or 0) or None
    except (TypeError, ValueError):
        n = None
    picked = exam.pick_quiz(paths, mod, topic, n)
    if not picked:
        return _C["no_cards"].format(**c)
    item = exam.set_quiz(paths, mod, picked, topic, dt.date.today())
    questions = "\n".join(_C["quiz_question"].format(**card) for card in item["cards"])
    return _C["quiz"].format(n=len(picked), topic=_C["quiz_topic"].format(topic=topic) if topic else "",
                             questions=questions, **c)


def round_offer(round_no: int) -> str:
    """study_mode's hook: the end-of-round check, offered only when some
    module has cards to quiz from."""
    try:
        paths = _paths()
        from . import cards as _cards
        if not any(_cards.all_cards(_cards.load(paths, m)) for m in conf.modules(paths)):
            return ""
    except Exception:  # noqa: BLE001 -- no vault, no offer
        return ""
    return CONFIG["quiz"]["offer"].format(round=round_no, n=CONFIG["quiz"]["questions"], **_ctx())


# -- weak topics ---------------------------------------------------------------------------

def _learner_claims(mod: Module) -> str:
    try:
        core = CORE
        if core is None:
            from learner import core
        data = core.load()
    except Exception:  # noqa: BLE001
        return ""
    area = CONFIG["learner"]["area"]
    claims = [b["claim"] for b in data.get("beliefs", []) if b.get("status") == "current"
              and b.get("area") == area and mod.title.lower() in b["claim"].lower()]
    return _C["weak_learner"].format(claims=_N["list_sep"].join(claims[:_T["learner_max"]])) if claims else ""


def weak_topics(module: str = "") -> str:
    paths = _paths()
    mod, line = _resolve(paths, module)
    if mod is None:
        return line
    c = _ctx(mod)
    stats = exam._stats(paths, mod)
    if not stats.get("topics"):
        return _C["no_stats"].format(**c)
    weak = [t for t in stats["topics"] if t.get("weak")]
    learner = _learner_claims(mod)
    if not weak:
        return _C["weak_none"].format(cards=stats.get("cards", 0), reviewed=stats.get("reviewed", 0),
                                      date=stats.get("synced", ""), learner=learner, **c)
    parts = []
    for t in weak:
        weeks = _C["weak_weeks"].format(weeks=_N["list_sep"].join(map(str, t["weeks"]))) if t.get("weeks") else ""
        parts.append(_C["weak_topic"].format(topic=t["topic"], lapses=t["lapses"], reviewed=t["reviewed"], weeks=weeks))
    return _C["weak"].format(topics=_N["list_sep"].join(parts), date=stats.get("synced", ""), learner=learner, **c)


# -- exam practice ---------------------------------------------------------------------------

def exam_question(module: str = "", topic: str = "") -> str:
    paths = _paths()
    topic = " ".join((topic or "").split())
    mod, line = _resolve(paths, module, topic)
    if mod is None:
        return line
    c = _ctx(mod)
    try:
        item = exam.set_exam(paths, mod, topic, calls=CALLS, today=dt.date.today())
    except model.ModelError as exc:
        return _C["model_failed"].format(what=_C["what_question"], why=exc, **c)
    rec = exam.write_open(paths, mod, dt.date.today(), say=_say)
    _commit(paths, rec["touched"], "practice", mod)
    style = (_C["exam_style"].format(names=_N["list_sep"].join(item["style"])) if item["style"]
             else _C["exam_style_none"])
    return _C["exam"].format(topic=_C["quiz_topic"].format(topic=topic) if topic else "", marks=item["marks"],
                             style=style, title=item["title"], question=item["question"], **c)


def _latest_open(paths: Paths) -> Module | None:
    """With no module named: the module whose open item was set last."""
    best, when = None, ""
    for m in conf.modules(paths):
        item = exam.open_item(paths, m)
        if item and item.get("date", "") >= when:
            best, when = m, item.get("date", "")
    return best


def mark_answer(answer: str, module: str = "") -> str:
    paths = _paths()
    today = dt.date.today()
    answer = (answer or "").strip()
    mod = _latest_open(paths) if not (module or "").strip() else None
    if mod is None:
        mod, line = _resolve(paths, module)
        if mod is None:
            return line
    c = _ctx(mod)
    item = exam.open_item(paths, mod)
    if not item:
        return _C["no_open"].format(**c)
    note_name = _note_link(exam.note_path(paths, mod))
    if item["kind"] == exam.KIND_QUIZ:
        try:
            result = exam.mark_quiz(mod, item, answer, calls=CALLS)
        except model.ModelError as exc:
            return _C["mark_failed"].format(why=exc, **c)
        rec = exam.record(paths, mod, {"kind": exam.KIND_QUIZ, "topic": item.get("topic", ""), "answer": answer,
                                       **result}, today, say=_say)
        _commit(paths, rec["touched"], "practice", mod)
        lines = []
        for r in result["results"]:
            if r["correct"] is None:
                lines.append(_C["quiz_unanswered"].format(**r))
            else:
                lines.append(_C["quiz_result"].format(mark=_C["quiz_right"] if r["correct"] else _C["quiz_wrong"], **r))
        return _C["quiz_marked"].format(right=result["right"], answered=result["answered"], asked=result["asked"],
                                        results="\n".join(lines), note=_note_link(rec["note"]) or note_name, **c)
    engine = CONFIG["exam"]["marking_engine"]

    def work():
        try:
            result = exam.mark_exam(mod, item, answer, calls=CALLS, engine=engine)
        except model.ModelError as exc:
            return _C["mark_failed"].format(why=exc, **c)
        rec = exam.record(paths, mod, {"kind": exam.KIND_EXAM, "title": item["title"], "question": item["question"],
                                       "topic": item.get("topic", ""), "answer": answer, **result}, today, say=_say)
        _commit(paths, rec["touched"], "practice", mod)
        steps = "\n".join(_C["marked_step"].format(**s) for s in result["steps"])
        return _C["marked"].format(marks=result["marks"], total=result["total"], title=item["title"], steps=steps,
                                   feedback=result["feedback"], note=_note_link(rec["note"]) or note_name, **c)
    via_delegate = CONFIG["cards"]["engines"].get(engine, {}).get("via") == "delegate"
    if via_delegate and (CALLS is None or engine not in CALLS):
        if _dry_run():
            return _C["marking"].format(note=note_name, **c)
        _hand_off(work)
        return _C["marking"].format(note=note_name, **c)
    return work()
