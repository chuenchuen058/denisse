"""python -m tutor cards --note PATH --deck NAME: cards from one study note.

Chuen (10 Nov 2026): a research mini-project's teaching doc, with its own
self-check questions, as Anki cards, so he can defend every concept. The
note may be anywhere (in the vault or not); it is only read.

The cards are made by the module path's own pipeline (cards.generate, then
cards.check), with its kinds, prompts and limits; a note longer than one
model call is read in parts (model.chunks), each its own source. The
self-check questions the note sets are copied as cards of the kind
cards.note.self_check.kind: the question word for word, and on the back the
note's own answer when it gives one (also found in the note), else the
no_answer line -- the model never writes a missing answer. A question-only
card makes no claim, so it isn't sent to the checker.

The deck is a NoteDeck: a Module whose title is the deck name, so anki.py
puts it under the tutor's parent deck. Its card store is cards.note.card_file
in the Don data folder: per part, the note's path, its content hash and its
cards; a re-run asks the model again only for a part whose text changed, and
card ids are stable, so Anki updates rather than duplicates. Nothing is
written outside the Don area.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os

from . import anki, cards, conf, don, model, rules, sources
from .conf import CONFIG, Module, Paths

_C = CONFIG["cards"]
_N = _C["note"]
_SC = _N["self_check"]
_V = _C["check"]["verdicts"]
_SAY = CONFIG["say"]


@dataclasses.dataclass
class NoteDeck(Module):
    card_file: str = ""


def deck(name: str, note_path: str = "") -> NoteDeck:
    title = " ".join(str(name or "").split())
    return NoteDeck(folder=os.path.dirname(os.path.abspath(note_path)) if note_path else "", code="", title=title,
                    card_file=_N["card_file"].format(slug=conf.slug(title)))


def pending_key(module: Module) -> str:
    return _N["pending_key"].format(slug=module.slug)


def known(paths: Paths) -> list:
    """Every study-note deck with a card store in the Don data folder."""
    out = []
    folder = paths.data_dir
    if not os.path.isdir(folder):
        return out
    prefix, _sep, suffix = _N["card_file"].partition("{slug}")
    for name in sorted(os.listdir(folder)):
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        store = don.load_json(paths, name) or {}
        if store.get("deck"):
            d = deck(store["deck"])
            if d.card_file == name:
                out.append(d)
    return out


def rel_of(paths: Paths, path: str) -> str:
    """The note's key: vault-relative ('/'-separated) inside the vault, else its absolute path."""
    full = os.path.abspath(path)
    vault = os.path.abspath(paths.vault)
    try:
        inside = os.path.commonpath([os.path.normcase(full), os.path.normcase(vault)]) == os.path.normcase(vault)
    except ValueError:  # another drive
        inside = False
    return (os.path.relpath(full, vault) if inside else full).replace(os.sep, "/")


def read(paths: Paths, path: str) -> sources.Source | None:
    """The note as one Source (frontmatter removed), or None when it can't be read or is empty."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            raw = f.read()
    except OSError:
        return None
    fm, body = sources.frontmatter(raw)
    if not body.strip():
        return None
    week = fm.get("week")
    return sources.Source(path=os.path.abspath(path), rel=rel_of(paths, path), kind=CONFIG["sources"]["own"],
                          week=int(week) if str(week or "").isdigit() else None, topic=fm.get("topic", ""),
                          text=body)


def parts(src: sources.Source, limit: int) -> list:
    """The note in pieces one model call can read; one piece keeps the note's own key."""
    pieces = model.chunks(src.text, max(1, limit))
    if len(pieces) == 1:
        return [src]
    return [dataclasses.replace(src, rel=_N["part_key"].format(rel=src.rel, n=n), text=text)
            for n, text in enumerate(pieces, 1)]


# -- the note's own self-check questions ---------------------------------------------

def in_note(quote, text: str) -> bool:
    """`quote` is in the note: its words in order, or backed_by's share of them."""
    q = " ".join(cards._words(quote))
    return bool(q) and (f" {q} " in f" {' '.join(cards._words(text))} " or cards.backed_by(quote, text))


def self_checks(module: Module, src: sources.Source, calls: dict | None = None,
                say=print, name: str = "") -> list:
    """The self-check questions `src` sets, as cards (unchecked). A question
    not found in the note is left out; an answer not found in it is not used."""
    kind = _SC["kind"]
    c = don.ctx(module)
    with model.job("cards"):
        data = cards._ask(cards.engine_call(_SC["engine"], calls), _SC["system"].format(**c),
                          _SC["user"].format(source=src.stem, schema=_SC["schema"], text=src.text, **c),
                          _SC["schema"])
    out, fronts, dropped = [], set(), 0
    for item in data.get("questions") or []:
        if not isinstance(item, dict):
            continue
        front = cards._clip(item.get("question"), cards._limit(kind, "front_chars"))
        key = " ".join(cards._words(front))
        if not front or key in fronts:
            continue
        if not in_note(front, src.text):
            dropped += 1
            continue
        if len(out) >= _SC["max"]:
            break
        fronts.add(key)
        answer = cards._clip(item.get("answer"), cards._limit(kind, "back_chars"))
        given = bool(answer) and in_note(answer, src.text)
        out.append({"id": cards.card_id(module, src.rel, kind, len(out) + 1), "kind": kind, "front": front,
                    "back": answer if given else _SC["no_answer"].format(source=src.stem),
                    "topic": cards._clip(item.get("topic"), _C["topic_chars"]) or src.topic or src.stem,
                    "source": src.rel, "source_name": src.stem, "week": src.week, "engine": _SC["engine"],
                    "answer_in_note": given})
    say(_N["say"]["self_checks"].format(name=name or src.stem, n=len(out),
                                        answered=sum(1 for card in out if card["answer_in_note"]), dropped=dropped))
    return out


def _unanswered(card: dict, when: str) -> dict:
    return cards._verdict(cards.as_written(card), _SC["unanswered_checker"], _V["right"], _SC["unanswered_reason"],
                          when)


# -- the job -------------------------------------------------------------------------

def run(paths: Paths, note_path: str, deck_name: str, calls: dict | None = None, checks: dict | None = None,
        today: dt.date | None = None, say=print, num_ctx: int | None = None) -> dict:
    """Cards for one note into `deck_name`'s card store. Returns {module,
    cards (every checked card of the deck), made, kept, failed, held,
    touched}; None when the note can't be read."""
    today = today or dt.date.today()
    module = deck(deck_name, note_path)
    src = read(paths, note_path)
    if src is None:
        say(_N["say"]["no_note"].format(path=note_path, **don.ctx()))
        return None
    rule = _N["rule"].format(**don.ctx(module))
    store = cards.load(paths, module)
    store["deck"] = module.title
    limit = model.chunk_chars(num_ctx) - len(rules.block("cards")) - len(rule)
    pieces = parts(src, limit)
    made, kept, failed, held_all = [], [], [], []
    # every earlier part of this note goes; the current ones are written below
    old = {k: v for k, v in store["sources"].items() if v.get("note") == src.rel or k == src.rel}
    for k in old:
        del store["sources"][k]
    for part in pieces:
        dropped = []
        digest = cards._hash(part.text)
        prev = old.get(part.rel)
        earlier = (prev or {}).get("cards", []) + (prev or {}).get("held", [])
        before = {cards.check_key(card): card for card in earlier}
        name = part.rel.replace(src.rel, src.stem, 1)
        if (prev and prev.get("hash") == digest
                and set(cards.active_kinds()) <= set(prev.get("kinds_asked") or [])):
            written, entry = [cards.as_written(card) for card in earlier], dict(prev)
            kept.append(part.rel)
            say(_SAY["cards_source"].format(status="unchanged", name=name, n=len(earlier)))
        else:
            try:
                written = cards.generate(module, part, calls, num_ctx, dropped=dropped, paths=paths, rule=rule)
                written += self_checks(module, part, calls, say=say, name=name)
            except (model.ModelError, OSError) as exc:
                failed.append((part.rel, str(exc)))
                say(_SAY["cards_failed"].format(name=name, why=exc))
                if not prev:
                    continue
                written, entry = [cards.as_written(card) for card in earlier], dict(prev)  # the old cards stand
            else:
                entry = {"hash": digest, "note": src.rel, "path": src.path, "kind": src.kind, "week": src.week,
                         "made": today.isoformat(), "kinds_asked": cards.active_kinds()}
                made.append(part.rel)
                say(_SAY["cards_source"].format(status="made", name=name, n=len(written)))
        for card in written:  # a part's cards cite the note itself
            card["source"], card["source_name"] = src.rel, src.stem
        if dropped:
            say(_SAY["cards_dropped"].format(name=name, n=len(dropped),
                                             why="; ".join(sorted({why for _k, _f, why in dropped}))))
            entry["dropped"] = len(dropped)
        when = dt.datetime.now().isoformat(timespec="seconds")
        bare = [(i, card) for i, card in enumerate(written) if card.get("answer_in_note") is False]
        rest = [card for card in written if card.get("answer_in_note") is not False]
        passed, held, counts = cards.check(module, part, rest, before, checks, num_ctx=num_ctx, paths=paths)
        if counts["reused"] < len(rest):
            say(_SAY["cards_checked"].format(name=name, **counts))
        entry["cards"] = passed + [_unanswered(card, when) for _i, card in bare]
        entry["held"] = held
        store["sources"][part.rel] = entry
        held_all += held
    store["updated"] = today.isoformat()
    touched = [don.save_json(paths, cards.sidecar(module), store)]
    checked = cards.checked_cards(store)
    status = "made" if made else "failed" if failed else "unchanged"
    say(_N["say"]["done"].format(n=len(checked), deck=anki.deck_name(module), source=src.stem, status=status,
                                 held=len(held_all), **don.ctx()))
    return {"module": module, "cards": checked, "made": made, "kept": kept, "failed": failed, "held": held_all,
            "touched": touched}


def sync(paths: Paths, module: Module, client=None, say=print) -> dict:
    """The deck to Anki by anki-sync's own path: the TSV, then, when
    AnkiConnect answers, add or update by card id. Returns {synced (counts,
    or None when it didn't reach Anki), touched}."""
    deck_cards = cards.checked_cards(cards.load(paths, module))
    out = {"synced": None, "touched": []}
    if not deck_cards:
        say(_SAY["no_cards"].format(module=module.title))
        return out
    if CONFIG["anki"]["tsv"]["enabled"]:
        path = anki.write_tsv(paths, module, deck_cards)
        out["touched"].append(path)
        say(_SAY["tsv"].format(path=path, n=len(deck_cards)))
    if not CONFIG["anki"]["connect"]["enabled"]:
        say(_SAY["anki_off"])
        return out
    client = client or anki.Connect()
    ok, why = client.reachable()
    if not ok:
        say(_SAY["anki_down"].format(url=getattr(client, "url", CONFIG["anki"]["connect"]["url"]), why=why))
        return out
    try:
        counts = anki.push(paths, client, module, deck_cards)
    except anki.AnkiError as exc:
        say(_SAY["anki_down"].format(url=getattr(client, "url", CONFIG["anki"]["connect"]["url"]), why=exc))
        return out
    say(_SAY["anki_sync"].format(deck=anki.deck_name(module), **counts))
    out["synced"] = counts
    return out


def record_sync(paths: Paths, module: Module, result: dict) -> str | None:
    """The rules' unsynced list: this deck leaves it when it reached Anki, joins it when it didn't."""
    if not rules.on("sync_after_cards") or not CONFIG["anki"]["connect"]["enabled"]:
        return None
    key = pending_key(module)
    return rules.mark_synced(paths, [key], []) if result["synced"] is not None \
        else rules.mark_synced(paths, [], [key])


# -- the battery rule and the GPU lease -------------------------------------------------

class Refused(RuntimeError):
    def __init__(self, line: str):
        super().__init__(line)
        self.line = line


def _real_power() -> dict:
    try:
        from orchestra import power
        return power.power_status()
    except Exception:  # noqa: BLE001 -- can't tell: counts as AC, as for the runner
        return {"ac": None}


def _real_lease():
    try:
        from orchestra.local_team.lease import GpuLease
    except Exception:  # noqa: BLE001 -- no lease module: nothing to share the GPU with
        return None
    g = _N["guard"]
    return GpuLease.for_repo(conf.REPO, g["lease_holder"], ttl_s=g["lease_ttl_s"])


class guarded:
    """`with guarded(deck):` the battery rule, then the shared GPU lease, held
    for the job. Raises Refused (its one line) on battery or when the lease
    is still held after lease_wait_s. `power()` -> status dict and `lease`
    (an object with acquire/release/info) are for tests."""

    def __init__(self, deck_title: str, power=None, lease=None):
        self.deck, self.power, self.lease = deck_title, power or _real_power, lease
        self.held = None

    def __enter__(self):
        g = _N["guard"]
        if g["battery"] and (self.power() or {}).get("ac") is False:  # None (unknown) counts as AC
            raise Refused(_N["say"]["on_battery"].format(deck=self.deck, **don.ctx()))
        if g["lease"]:
            lease = self.lease if self.lease is not None else _real_lease()
            if lease is not None:
                if not lease.acquire(wait_s=g["lease_wait_s"], poll_s=g["lease_poll_s"]):
                    info = lease.info() or {}
                    raise Refused(_N["say"]["lease_busy"].format(deck=self.deck, holder=info.get("holder", "?"),
                                                                 pid=info.get("pid", "?"), **don.ctx()))
                self.held = lease
        return self

    def __exit__(self, *exc):
        if self.held is not None:
            self.held.release()
        return False
