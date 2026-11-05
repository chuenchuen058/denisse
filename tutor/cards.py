"""python -m tutor cards <module>: questions from every source note.

For each of the module's source notes (sources.collect: the tutor notes
ingest wrote, his own notes, anything in the vault tagged or linked to the
module) the model writes cards of the kinds in tutor.json cards.kinds:
recall (definitions, equations), method (which method and why) and short
worked problems. Each kind names its engine: `local` is Ollama (the same
model entry ingest uses), `claude` hands the prompt to delegate.py (his
Claude plan through the claude CLI, never an API key). A note is asked
again only when its text has changed.

Every card keeps the vault path of the note it came from, its module, week
and topic, and a stable id built from (module, source, kind, position), so
a re-sync to Anki updates a card instead of adding it twice.

Every card is then checked before it can reach Anki (tutor.json
cards.check): the physics or maths right, the back answering the front,
agreeing with the source note. Each kind names its checker (`check`;
default: recall by the local model, method and problem by Claude through
delegate.py); `check.all_with` sends every kind to one engine. A wrong card
is corrected when the checker gives a corrected back and quotes the note
sentence that backs it (the quote must really be in the note); otherwise it
is held back: kept in the sidecar's `held` list and under "Held back" in the
cards note with the reason, never sent to Anki. Every card records its
check (by, verdict, when, reason, and the key of the card as written), so a
re-run checks only cards that are new or changed, whose checker changed, or
that could not be checked last time.

The cards live in the Don area: a JSON sidecar (cards-<slug>.json) and a
readable note listing each card under a link to its source.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re

from . import don, ingest, model, sources
from .conf import CONFIG, Module, Paths

_C = CONFIG["cards"]
_K = _C["check"]
_V = _K["verdicts"]
_PASS = (_V["right"], _V["fixed"])
_D = CONFIG["don"]
_SAY = CONFIG["say"]


def _clip(text, limit: int) -> str:
    return re.sub(r"[ \t]+", " ", str(text or "")).strip()[:limit]


def card_id(module: Module, rel: str, kind: str, n: int) -> str:
    key = _C["id_key"].format(module=module.slug, source=rel, kind=kind, n=n)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_C["id_hash_chars"]]
    return _C["id"].format(hash=digest, **don.ctx(module))


def engine_call(name: str, calls: dict | None = None):
    """call(system, user) -> str for an engine in cards.engines. `calls`
    (tests) overrides by engine name."""
    if calls and name in calls:
        return calls[name]
    engine = _C["engines"][name]
    if engine["via"] == "delegate":
        def call(system, user):
            import delegate
            ok, text, _engine, _failures = delegate.run(_C["delegate_task"].format(system=system, user=user),
                                                        engines=engine["delegate_engines"])
            if not ok:
                raise model.ModelError(text)
            return text
        return call
    return model.ollama_call


def _ask(call, system: str, user: str) -> dict:
    for attempt in range(1 + CONFIG["model"]["json_retries"]):
        data = model.parse(call(system, user if attempt == 0 else user + CONFIG["prompt"]["retry"]))
        if data is not None:
            return data
    raise model.ModelError(_SAY["cards_no_json"])


def generate(module: Module, src: sources.Source, calls: dict | None = None, num_ctx: int | None = None) -> list:
    """The cards for one source note, every kind, each from its engine."""
    by_engine = {}
    for kind, spec in _C["kinds"].items():
        if spec["count"] > 0:
            by_engine.setdefault(spec["engine"], []).append(kind)
    text = src.text[:model.chunk_chars(num_ctx)]
    c = don.ctx(module)
    system = _C["system"].format(**c)
    out = []
    for engine, kinds in by_engine.items():
        wanted = "\n".join(_C["wanted"].format(kind=k, **_C["kinds"][k]) for k in kinds)
        user = _C["user"].format(week=_C["week"].format(week=src.week) if src.week else "", source=src.stem,
                                 wanted=wanted, schema=_C["schema"], text=text, **c)
        data = _ask(engine_call(engine, calls), system, user)
        counts = {k: 0 for k in kinds}
        for item in data.get("cards") or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip().lower()
            front, back = _clip(item.get("front"), _C["front_chars"]), _clip(item.get("back"), _C["back_chars"])
            if kind not in counts or not front or not back or counts[kind] >= _C["kinds"][kind]["count"]:
                continue
            counts[kind] += 1
            out.append({"id": card_id(module, src.rel, kind, counts[kind]), "kind": kind, "front": front,
                        "back": back,
                        "topic": _clip(item.get("topic"), _C["topic_chars"]) or src.topic or src.stem,
                        "source": src.rel, "source_name": src.stem, "week": src.week, "engine": engine})
    return out


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sidecar(module: Module) -> str:
    return _D["card_file"].format(slug=module.slug)


def load(paths: Paths, module: Module) -> dict:
    deck = don.load_json(paths, sidecar(module)) or {}
    deck.setdefault("module", module.title)
    deck.setdefault("code", module.code)
    deck.setdefault("sources", {})
    return deck


def all_cards(deck: dict) -> list:
    """Every card of the deck, a question asked twice (two notes, same
    front) only once: Anki would refuse the second as a duplicate."""
    out, fronts = [], set()
    for src in deck.get("sources", {}).values():
        for card in src.get("cards", []):
            key = " ".join(card["front"].lower().split())
            if key not in fronts:
                fronts.add(key)
                out.append(card)
    return out


def checked_cards(deck: dict) -> list:
    """all_cards, only those whose check passed: what may go to Anki."""
    return [card for card in all_cards(deck) if (card.get("check") or {}).get("verdict") in _PASS]


# -- the check ---------------------------------------------------------------------

def check_engine(kind: str) -> str:
    return _K["all_with"] or _C["kinds"][kind]["check"]


def as_written(card: dict) -> dict:
    """The card as its writer gave it: any correction and check undone."""
    out = {k: v for k, v in card.items() if k not in ("check", "written_back")}
    if "written_back" in card:
        out["back"] = card["written_back"]
    return out


def check_key(card: dict) -> str:
    """Identifies the card as written, so an unchanged card is not re-checked."""
    w = as_written(card)
    return _hash(_K["key"].format(kind=w["kind"], front=w["front"], back=w["back"]))


def _words(text) -> list:
    return re.findall(_K["word"], str(text or "").lower())


def backed_by(evidence, text: str) -> bool:
    """The checker's quote is really in the note: at least
    evidence_min_words words, and that share of them found in the note in
    the same order."""
    quote = _words(evidence)
    if len(quote) < _K["evidence_min_words"]:
        return False
    import difflib
    blocks = difflib.SequenceMatcher(None, quote, _words(text), autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) >= _K["evidence_min_overlap"] * len(quote)


def _verdict(card: dict, engine: str, verdict: str, reason, when: str) -> dict:
    card["check"] = {"by": engine, "verdict": verdict, "when": when,
                     "reason": _clip(reason, _K["reason_chars"]), "key": check_key(card)}
    return card


def _judge(card: dict, answer, engine: str, text: str, when: str) -> dict:
    """One card, one answer from the checker -> the card with its check."""
    reasons = _K["reasons"]
    verdict = str((answer or {}).get("verdict", "")).strip().lower() if isinstance(answer, dict) else ""
    if verdict == _K["right"]:
        return _verdict(card, engine, _V["right"], answer.get("reason"), when)
    if verdict != _K["wrong"]:
        return _verdict(card, engine, _V["unchecked"], reasons["no_verdict"], when)
    reason = _clip(answer.get("reason"), _K["reason_chars"])
    corrected = _clip(answer.get("corrected"), _C["back_chars"])
    if not corrected:
        return _verdict(card, engine, _V["wrong"], reasons["no_fix"].format(reason=reason), when)
    if not backed_by(answer.get("evidence"), text):
        return _verdict(card, engine, _V["wrong"], reasons["fix_unsupported"].format(reason=reason), when)
    card["written_back"] = card["back"]
    card["back"] = corrected
    return _verdict(card, engine, _V["fixed"], reason, when)


def _ask_checks(call, system: str, user: str) -> list:
    for attempt in range(1 + CONFIG["model"]["json_retries"]):
        data = model.parse(call(system, user if attempt == 0 else user + CONFIG["prompt"]["retry"]))
        if data is not None and isinstance(data.get("checks"), list):
            return data["checks"]
    raise model.ModelError(_SAY["cards_no_json"])


def check(module: Module, src: sources.Source, written: list, before: dict, checks: dict | None = None,
          when: str | None = None, num_ctx: int | None = None) -> tuple:
    """Check `written` (cards as their writer gave them) against `src`.
    `before`: check key -> the card as it stood after an earlier check,
    reused when the same checker would check it again. `checks` (tests)
    overrides checker engines by name. Returns (passed, held, counts)."""
    when = when or dt.datetime.now().isoformat(timespec="seconds")
    text = src.text[:model.chunk_chars(num_ctx)]
    c = don.ctx(module)
    done, pending = [], {}
    for i, card in enumerate(written):
        engine = check_engine(card["kind"])
        old = before.get(check_key(card))
        chk = (old or {}).get("check") or {}
        if chk.get("by") == engine and chk.get("verdict") != _V["unchecked"]:
            done.append((i, {**card, **{k: old[k] for k in ("back", "written_back", "check") if k in old}}))
        else:
            pending.setdefault(engine, []).append((i, as_written(card)))
    reused = len(done)
    system = _K["system"].format(**c)
    for engine, batch in pending.items():
        items = "\n".join(_K["item"].format(n=n, kind=card["kind"], front=card["front"], back=card["back"])
                          for n, (_i, card) in enumerate(batch, 1))
        user = _K["user"].format(week=_C["week"].format(week=src.week) if src.week else "", source=src.stem,
                                 items=items, schema=_K["schema"], right=_K["right"], wrong=_K["wrong"],
                                 text=text, **c)
        try:
            replies = _ask_checks(engine_call(engine, checks), system, user)
        except (model.ModelError, OSError) as exc:
            why = _K["reasons"]["unreachable"].format(engine=engine, why=exc)
            done += [(i, _verdict(card, engine, _V["unchecked"], why, when)) for i, card in batch]
            continue
        answers = {}
        for item in replies:
            try:
                answers.setdefault(int(item.get("n")), item)
            except (AttributeError, TypeError, ValueError):
                continue
        done += [(i, _judge(card, answers.get(n), engine, text, when)) for n, (i, card) in enumerate(batch, 1)]
    done.sort(key=lambda pair: pair[0])
    passed = [card for _i, card in done if card["check"]["verdict"] in _PASS]
    held = [card for _i, card in done if card["check"]["verdict"] not in _PASS]
    counts = {"reused": reused, "held": len(held),
              "right": sum(1 for card in passed if card["check"]["verdict"] == _V["right"]),
              "fixed": sum(1 for card in passed if card["check"]["verdict"] == _V["fixed"])}
    return passed, held, counts


def render_note(paths: Paths, module: Module, deck: dict, today: dt.date) -> str:
    c = don.ctx(module, today=today.isoformat(), name=_D["cards_name"].format(**don.ctx(module)),
                deck=CONFIG["anki"]["deck"].format(**don.ctx(module)))
    lines = [line.format(links=don._links(paths, module, don.cards_path(paths, module)), **c)
             for line in _D["cards_front"]]
    any_ = False
    for rel, src in deck["sources"].items():
        if not src.get("cards"):
            continue
        any_ = True
        stem = os.path.splitext(rel.split("/")[-1])[0]
        link = CONFIG["sources"]["source_link"].format(rel=os.path.splitext(rel)[0], stem=stem)
        lines += [_D["cards_source_heading"].format(link=link), ""]
        for card in src["cards"]:
            chk = card.get("check") or {}
            mark = _D["cards_fixed_mark"] if chk.get("verdict") == _V["fixed"] else _D["cards_checked_mark"]
            lines.append(_D["cards_card"].format(
                kind=card["kind"], front=_shown(card["front"]), back=_shown(card["back"]),
                topic=_D["cards_topic"].format(topic=card["topic"]) if card.get("topic") else "")
                + (mark.format(by=chk["by"]) if chk.get("by") else ""))
        lines.append("")
    if not any_:
        lines.append(_D["cards_none"])
    held = [(rel, card) for rel, src in deck["sources"].items() for card in src.get("held", [])]
    if held:
        lines += ["", _D["cards_held_heading"], "", _D["cards_held_intro"], ""]
        for rel, card in held:
            chk = card.get("check") or {}
            stem = os.path.splitext(rel.split("/")[-1])[0]
            link = CONFIG["sources"]["source_link"].format(rel=os.path.splitext(rel)[0], stem=stem)
            lines.append(_D["cards_held_card"].format(
                kind=card["kind"], front=_shown(card["front"]), back=_shown(card["back"]),
                reason=chk.get("reason", ""), by=chk.get("by", ""), when=chk.get("when", ""), link=link))
    return "\n".join(lines).rstrip("\n") + "\n"


def _shown(text: str) -> str:
    """One line of card text with its maths in the delimiters the vault renders."""
    from . import mathfmt
    return mathfmt.normalise(" ".join(str(text or "").split()), CONFIG["don"]["math_target"])


def run(paths: Paths, module: Module, calls: dict | None = None, today: dt.date | None = None,
        say=print, num_ctx: int | None = None, checks: dict | None = None) -> dict:
    """Cards for every source note of `module` that changed since last time,
    each checked before it is kept (`checks`, tests: checker engines by name).
    Returns {cards, made, kept, failed, held, touched}; `cards` is only the
    checked ones, what anki-sync sends."""
    today = today or dt.date.today()
    deck = load(paths, module)
    found = sources.collect(paths, module)
    say(_SAY["sources"].format(module=module.title, n=len(found),
                               own=sum(1 for s in found if s.kind == CONFIG["sources"]["own"])))
    made, kept, failed, held_all = [], [], [], []
    fresh = {}
    for src in found:
        digest = _hash(src.text)
        prev = deck["sources"].get(src.rel)
        earlier = (prev or {}).get("cards", []) + (prev or {}).get("held", [])
        before = {check_key(card): card for card in earlier}
        if prev and prev.get("hash") == digest:
            written, entry = [as_written(card) for card in earlier], dict(prev)
            kept.append(src.rel)
            say(_SAY["cards_source"].format(status="unchanged", name=src.stem, n=len(earlier)))
        else:
            try:
                written = generate(module, src, calls, num_ctx)
            except (model.ModelError, OSError) as exc:
                failed.append((src.rel, str(exc)))
                say(_SAY["cards_failed"].format(name=src.stem, why=exc))
                if not prev:
                    continue
                # The old cards stand until the note gives new ones (still checked, below).
                written, entry = [as_written(card) for card in earlier], dict(prev)
            else:
                entry = {"hash": digest, "kind": src.kind, "week": src.week, "made": today.isoformat()}
                made.append(src.rel)
                say(_SAY["cards_source"].format(status="made", name=src.stem, n=len(written)))
        passed, held, counts = check(module, src, written, before, checks, num_ctx=num_ctx)
        if counts["reused"] < len(written):
            say(_SAY["cards_checked"].format(name=src.stem, **counts))
        entry["cards"], entry["held"] = passed, held
        fresh[src.rel] = entry
        held_all += held
    deck["sources"] = fresh
    deck["updated"] = today.isoformat()
    touched = [don.save_json(paths, sidecar(module), deck)]
    state = ingest.load_state(paths)
    path = don.write_note(don.cards_path(paths, module), render_note(paths, module, deck, today), state, say)
    if path:
        say(_SAY["cards_note"].format(path=path))
        touched.append(path)
    touched += don.refresh(paths, state, today=today, say=say)
    ingest.save_state(paths, state)
    touched.append(paths.state_file())
    cards = checked_cards(deck)
    say(_SAY["cards_done"].format(module=module.title, n=len(cards), sources=len(fresh), made=len(made),
                                  kept=len(kept), failed=len(failed), held=len(held_all)))
    return {"cards": cards, "made": made, "kept": kept, "failed": failed, "held": held_all, "touched": touched}

