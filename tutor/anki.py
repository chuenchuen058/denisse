"""python -m tutor anki-sync: cards out to Anki, review stats back in.

Two ways out, both switched in tutor.json "anki":

  tsv      one Anki-importable TSV per module in the Don area (File >
           Import in Anki). The card id is its GUID column, so importing a
           newer file updates cards instead of adding them twice.
  connect  AnkiConnect (the add-on's local HTTP API, URL in tutor.json).
           When it answers: the deck (<name>::<module>) and the note type
           (Front/Back/Source/Topic/Id) are made if missing, each card is
           added or, matched by its Id field, updated -- never duplicated --
           and the review stats (lapses, ease, due) are read back per topic.

Weak topics (tutor.json "weak") go into the anki-<slug>.json sidecar and
the module's progress note, and into the learner model as measured beliefs
(don.feed_learner). With AnkiConnect off or not reachable, the TSV is
written and nothing is recorded: no stats is not the same as no lapses.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import string
import urllib.parse
import urllib.request

from . import cards, conf, don, ingest
from .conf import CONFIG, Module, Paths

_A = CONFIG["anki"]
_K = _A["connect"]
_W = CONFIG["weak"]
_D = CONFIG["don"]
_SAY = CONFIG["say"]


class AnkiError(RuntimeError):
    pass


class Connect:
    """A minimal AnkiConnect client. No proxy: it is always a local port."""

    def __init__(self, url: str | None = None, version: int | None = None, timeout: float | None = None):
        self.url = url or _K["url"]
        self.version = version or _K["version"]
        self.timeout = timeout or _K["timeout_s"]
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    def __call__(self, action: str, **params):
        body = json.dumps({"action": _K["actions"][action], "version": self.version, "params": params})
        req = urllib.request.Request(self.url, data=body.encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise AnkiError(str(exc)) from exc
        if not isinstance(data, dict):
            raise AnkiError(repr(data)[:200])
        if data.get("error"):
            raise AnkiError(str(data["error"]))
        return data.get("result")

    def reachable(self) -> tuple:
        try:
            self("version")
            return True, ""
        except AnkiError as exc:
            return False, str(exc)


# -- what a card looks like in Anki ---------------------------------------------

def deck_name(module: Module) -> str:
    return _A["deck"].format(**don.ctx(module))


def note_type() -> str:
    return _A["note_type"].format(**don.ctx())


def _html(text: str) -> str:
    return "<br>".join(html.escape(line, quote=False) for line in str(text or "").splitlines())


def fields(paths: Paths, card: dict) -> dict:
    stem = card.get("source_name") or os.path.splitext(card["source"].split("/")[-1])[0]
    source = _A["source_html"].format(vault=urllib.parse.quote(os.path.basename(os.path.normpath(paths.vault))),
                                      file=urllib.parse.quote(card["source"]), stem=html.escape(stem))
    values = {"front": _html(card["front"]), "back": _html(card["back"]), "source": source,
              "topic": html.escape(card.get("topic") or ""), "id": card["id"]}
    return {_A["field_of"][k]: v for k, v in values.items()}


def tags(module: Module, card: dict) -> list:
    c = don.ctx(module, kind=card["kind"], week=card.get("week"), topic_slug=conf.slug(card.get("topic", "")))
    out = []
    for tpl in _A["tags"]:
        names = [f[1] for f in string.Formatter().parse(tpl) if f[1]]
        if any(c.get(n) in (None, "") for n in names):
            continue  # no week, no topic: no tag rather than 'week::None'
        out.append("_".join(tpl.format(**c).split()))
    return out


# -- the TSV -------------------------------------------------------------------

def tsv_path(paths: Paths, module: Module) -> str:
    return os.path.join(paths.don_dir, _D["tsv_dir"], _D["tsv_file"].format(**don.ctx(module)))


def _cell(text: str) -> str:
    return " ".join(str(text).replace("\t", " ").splitlines())


def write_tsv(paths: Paths, module: Module, deck_cards: list) -> str:
    t = _A["tsv"]
    lines = [h.format(deck=deck_name(module), note_type=note_type()) for h in t["header"]]
    for card in deck_cards:
        f = fields(paths, card)
        values = {k: f[_A["field_of"][k]] for k in _A["field_of"]}
        values["tags"] = " ".join(tags(module, card))
        lines.append("\t".join(_cell(values[col]) for col in t["columns"]))
    path = tsv_path(paths, module)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


# -- AnkiConnect -----------------------------------------------------------------

def _query(tpl: str, module: Module) -> str:
    return tpl.format(note_type=note_type(), deck=deck_name(module))


def ensure(client, module: Module) -> None:
    client("create_deck", deck=deck_name(module))  # a no-op in Anki when it exists
    if note_type() not in (client("model_names") or []):
        client("create_model", modelName=note_type(), inOrderFields=_A["fields"], css=_A["css"],
               isCloze=False, cardTemplates=_A["templates"])


def push(paths: Paths, client, module: Module, deck_cards: list) -> dict:
    """Add the cards Anki hasn't got, update the ones whose fields changed,
    matched by the Id field. Counts: added, updated, same, failed."""
    ensure(client, module)
    id_field = _A["field_of"]["id"]
    existing = {}
    ids = client("find_notes", query=_query(_K["query_notes"], module)) or []
    for info in (client("notes_info", notes=ids) if ids else []) or []:
        f = {k: v.get("value", "") for k, v in (info.get("fields") or {}).items()}
        if f.get(id_field):
            existing[f[id_field]] = (info["noteId"], f)
    out = {"added": 0, "updated": 0, "same": 0, "failed": 0, "errors": []}
    for card in deck_cards:
        want = fields(paths, card)
        try:
            if card["id"] in existing:
                note_id, have = existing[card["id"]]
                if all(have.get(k) == v for k, v in want.items()):
                    out["same"] += 1
                    continue
                client("update_fields", note={"id": note_id, "fields": want})
                out["updated"] += 1
            else:
                client("add_note", note={"deckName": deck_name(module), "modelName": note_type(), "fields": want,
                                         "tags": tags(module, card),
                                         "options": {"allowDuplicate": False,
                                                     "duplicateScope": _K["duplicate_scope"]}})
                out["added"] += 1
        except AnkiError as exc:
            out["failed"] += 1
            out["errors"].append(f"{card['id']}: {exc}")
    return out


def review_stats(client, module: Module, deck_cards: list, today: dt.date) -> dict:
    """Per-topic review stats for the module's deck, weak topics marked."""
    ids = client("find_cards", query=_query(_K["query_cards"], module)) or []
    infos = (client("cards_info", cards=ids) if ids else []) or []
    due = (client("are_due", cards=[i["cardId"] for i in infos]) if infos else []) or []
    weeks = {}
    for card in deck_cards:
        if card.get("week"):
            weeks.setdefault((card.get("topic") or "").lower(), set()).add(card["week"])
    topic_field = _A["field_of"]["topic"]
    by_topic = {}
    for n, info in enumerate(infos):
        topic = html.unescape((info.get("fields") or {}).get(topic_field, {}).get("value", "")).strip()
        rec = by_topic.setdefault(topic.lower(), {"topic": topic, "cards": 0, "reviewed": 0, "reviews": 0,
                                                  "lapses": 0, "_ease": [], "due": 0})
        rec["cards"] += 1
        if (info.get("reps") or 0) > 0:
            rec["reviewed"] += 1
            rec["reviews"] += info.get("reps") or 0
            rec["lapses"] += info.get("lapses") or 0
            if info.get("factor"):
                rec["_ease"].append(info["factor"] / _W["ease_scale"])
        if n < len(due) and due[n]:
            rec["due"] += 1
    topics = []
    for key, rec in by_topic.items():
        ease = round(sum(rec["_ease"]) / len(rec["_ease"])) if rec["_ease"] else None
        rec.pop("_ease")
        rec["ease"] = ease
        rate = rec["lapses"] / rec["reviewed"] if rec["reviewed"] else 0.0
        rec["weak"] = bool(rec["reviewed"]) and (rate >= _W["lapse_rate"]
                                                 or (ease is not None and ease < _W["ease_below"]))
        rec["score"] = round(_W["score_lapse"] * rate
                             + _W["score_ease"] * max(0, _W["ease_full"] - (ease if ease is not None else _W["ease_full"]))
                             + _W["score_due"] * (rec["due"] / rec["cards"] if rec["cards"] else 0), 3)
        rec["weeks"] = sorted(weeks.get(key, ()))
        topics.append(rec)
    topics.sort(key=lambda r: (not r["weak"], -r["score"], r["topic"].lower()))
    return {"module": module.title, "code": module.code, "deck": deck_name(module), "synced": today.isoformat(),
            "cards": len(infos), "reviewed": sum(t["reviewed"] for t in topics), "topics": topics,
            "weak": [t["topic"] for t in topics if t["weak"]]}


# -- the job -----------------------------------------------------------------------

def run(paths: Paths, modules: list | None = None, client=None, today: dt.date | None = None,
        say=print, learner_core=None) -> dict:
    """TSV for every module with cards; then, if AnkiConnect answers, push
    the cards and read the stats back. Returns {tsv, synced, stats, reachable, touched}."""
    today = today or dt.date.today()
    modules = conf.modules(paths) if modules is None else modules
    decks = [(m, cards.checked_cards(cards.load(paths, m))) for m in modules]
    decks = [(m, c) for m, c in decks if c]
    if not decks:
        for m in modules:
            say(_SAY["no_cards"].format(module=m.title))
    touched, tsvs = [], []
    if _A["tsv"]["enabled"]:
        for module, deck_cards in decks:
            path = write_tsv(paths, module, deck_cards)
            tsvs.append(path)
            touched.append(path)
            say(_SAY["tsv"].format(path=path, n=len(deck_cards)))
    reachable, stats, synced = False, {}, {}
    if decks and not _K["enabled"]:
        say(_SAY["anki_off"])
    elif decks:
        client = client or Connect()
        reachable, why = client.reachable()
        if not reachable:
            say(_SAY["anki_down"].format(url=getattr(client, "url", _K["url"]), why=why))
    if reachable:
        for module, deck_cards in decks:
            try:
                synced[module.slug] = push(paths, client, module, deck_cards)
                say(_SAY["anki_sync"].format(deck=deck_name(module), **synced[module.slug]))
                stats[module.slug] = review_stats(client, module, deck_cards, today)
            except AnkiError as exc:
                say(_SAY["anki_down"].format(url=getattr(client, "url", _K["url"]), why=exc))
                continue
            s = stats[module.slug]
            touched.append(don.save_json(paths, _D["anki_file"].format(slug=module.slug), s))
            say(_SAY["anki_stats"].format(deck=s["deck"], cards=s["cards"], reviewed=s["reviewed"],
                                          weak=len(s["weak"])))
            touched += don.feed_learner(module, s["topics"], today, say=say, core=learner_core)
        state = ingest.load_state(paths)
        touched += don.refresh(paths, state, modules, today, say=say)
        ingest.save_state(paths, state)
        touched.append(paths.state_file())
    return {"tsv": tsvs, "synced": synced, "stats": stats, "reachable": reachable, "touched": touched}
