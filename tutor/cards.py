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
            lines.append(_D["cards_card"].format(
                kind=card["kind"], front=" ".join(card["front"].split()), back=" ".join(card["back"].split()),
                topic=_D["cards_topic"].format(topic=card["topic"]) if card.get("topic") else ""))
        lines.append("")
    if not any_:
        lines.append(_D["cards_none"])
    return "\n".join(lines).rstrip("\n") + "\n"


def run(paths: Paths, module: Module, calls: dict | None = None, today: dt.date | None = None,
        say=print, num_ctx: int | None = None) -> dict:
    """Cards for every source note of `module` that changed since last time.
    Returns {cards, made, kept, failed, touched}."""
    today = today or dt.date.today()
    deck = load(paths, module)
    found = sources.collect(paths, module)
    say(_SAY["sources"].format(module=module.title, n=len(found),
                               own=sum(1 for s in found if s.kind == CONFIG["sources"]["own"])))
    made, kept, failed = [], [], []
    fresh = {}
    for src in found:
        digest = _hash(src.text)
        prev = deck["sources"].get(src.rel)
        if prev and prev.get("hash") == digest:
            fresh[src.rel] = prev
            kept.append(src.rel)
            say(_SAY["cards_source"].format(status="unchanged", name=src.stem, n=len(prev.get("cards", []))))
            continue
        try:
            new = generate(module, src, calls, num_ctx)
        except (model.ModelError, OSError) as exc:
            failed.append((src.rel, str(exc)))
            say(_SAY["cards_failed"].format(name=src.stem, why=exc))
            if prev:
                fresh[src.rel] = prev  # the old cards stand until the note gives new ones
            continue
        fresh[src.rel] = {"hash": digest, "kind": src.kind, "week": src.week, "cards": new,
                          "made": today.isoformat()}
        made.append(src.rel)
        say(_SAY["cards_source"].format(status="made", name=src.stem, n=len(new)))
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
    cards = all_cards(deck)
    say(_SAY["cards_done"].format(module=module.title, n=len(cards), sources=len(fresh), made=len(made),
                                  kept=len(kept), failed=len(failed)))
    return {"cards": cards, "made": made, "kept": kept, "failed": failed, "touched": touched}

