"""python -m tutor <command>

  ingest [--dry]          read the KEATS inbox folders, write tutor notes and topic maps
  topics MODULE           print a module's topic map (name, code or alias words)
  cards MODULE            make cards from every vault note of a module (changed notes only)
  anki-sync [MODULE]      write the Anki TSV per module; with AnkiConnect, add/update the
                          cards and read the review stats (weak topics) back
  explain MODULE TOPIC    the tutor's lesson on a topic, from the vault (part 3; also a chat tool)
  quiz MODULE [TOPIC]     a short check from the cards, weak topics first
  weak MODULE             what the Anki stats say he keeps losing
  exam MODULE [TOPIC]     an exam-style problem in the coursework / past-paper style
  mark MODULE ANSWER      mark his answer to the open quiz or exam question
  progress [MODULE]       where he is per module: lectures read vs on KEATS, topics and
                          Anki mastery, weak topics, coursework deadlines, what to study
                          next -- into the vault's study progress note and the hub table
  consult QUESTION...     the tutor's opinion for another agent (part 5): answer, confidence,
  [--module M] [--json]   sources, caveats, who answered -- grounded in the vault, recorded in
                          the Don area. --json prints the structured opinion. Exit 1 = no answer.

Every job first reads the rules note in the Don area (tutor/rules.py,
tutor.json "rules"): its text goes into the model prompts, its Exams table
scopes Anki and the quizzes. A module whose cards never reached Anki is
synced first; `cards` ends with an anki-sync; ingest and cards take the
local-model lock; each batch job writes one line in the day's Handoff note.

Every command takes --vault PATH and --state DIR (the old private state
folder, read until its files are moved into the vault). Every vault write is
committed in the vault's own git repo, only the files written.
--dry scans and matches only: no model call, nothing written.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import anki, cards, conf, don, ingest, progress, rules, topics
from .conf import CONFIG

CHAT_COMMANDS = ("explain", "quiz", "weak", "exam", "mark")


def _paths(a) -> conf.Paths:
    p = conf.paths() if not (a.vault and a.state) else None
    vault = a.vault or p.vault
    state = a.state or p.state
    shared = p.shared_inboxes if p else []
    return conf.Paths(vault=vault, state=state, shared_inboxes=shared)


def _module(paths, text):
    modules = conf.modules(paths)
    module = conf.find_module(text, modules)
    if module is None:
        print(CONFIG["say"]["no_module"].format(text=text, modules=", ".join(m.title for m in modules) or "-"))
    return module


def _anki_word(slug: str, out: dict) -> str:
    h = CONFIG["rules"]["handoff"]
    if not CONFIG["anki"]["connect"]["enabled"]:
        return h["anki_off"]
    return h["synced"] if slug in out.get("synced", {}) else h["unsynced"]


def _sync(paths, modules, client, learner_core, say=print, commit=True) -> dict:
    """anki-sync for `modules` (None: every one); a module whose cards
    didn't reach Anki is recorded as unsynced, one that did is cleared.
    commit=False leaves the files in out["touched"] for the caller's commit."""
    out = anki.run(paths, modules, client=client, learner_core=learner_core, say=say)
    touched = list(out["touched"])
    if rules.on("sync_after_cards") and CONFIG["anki"]["connect"]["enabled"]:
        missed = [slug for slug in out["decks"] if slug not in out["synced"]]
        changed = rules.mark_synced(paths, list(out["synced"]), missed)
        if changed:
            touched.append(changed)
    out["touched"] = touched
    if commit:
        don.commit(paths, touched, "anki", say=say)
    return out


def _sync_pending(paths, client, learner_core, say=print) -> None:
    """The modules left unsynced last time, synced before this job when
    AnkiConnect answers."""
    c = CONFIG["rules"]["sync_after_cards"]
    if not rules.on("sync_after_cards") or not CONFIG["anki"]["connect"]["enabled"]:
        return
    slugs = rules.pending(paths)
    if not slugs:
        return
    client = client or anki.Connect()
    ok, _why = client.reachable()
    if not ok:
        say(c["say_still"].format(n=len(slugs)))
        return
    say(c["say_pending"].format(n=len(slugs)))
    mods = [m for m in conf.modules(paths) if m.slug in slugs]
    gone = [s for s in slugs if s not in {m.slug for m in mods}]
    if gone:
        rules.mark_synced(paths, gone, [])
    if mods:
        _sync(paths, mods, client, learner_core, say=say)


def main(argv=None, anki_client=None, calls=None, learner_core=None, checks=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tutor", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("ingest", "topics", "cards", "anki-sync", "progress", "consult") + CHAT_COMMANDS:
        s = sub.add_parser(name)
        s.add_argument("--vault", default=None)
        s.add_argument("--state", default=None)
        if name == "ingest":
            s.add_argument("--dry", action="store_true")
        elif name == "consult":
            s.add_argument("question", nargs="+")
            s.add_argument("--module", default="")
            s.add_argument("--json", action="store_true")
        elif name in ("anki-sync", "progress"):
            s.add_argument("module", nargs="?", default=None)
        else:
            s.add_argument("module")
        if name in ("explain", "mark"):
            s.add_argument("text", nargs="+")
        elif name in ("quiz", "exam"):
            s.add_argument("text", nargs="*")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    paths = _paths(a)
    rules.load(paths, say=lambda line: print(line, file=sys.stderr))  # a log line, never in the output
    if a.cmd not in CONFIG["rules"]["sync_after_cards"]["not_before"] and not getattr(a, "dry", False):
        _sync_pending(paths, anki_client, learner_core)
    try:
        with rules.batch_lock(paths, a.cmd):
            return _run(a, paths, anki_client, calls, learner_core, checks)
    except rules.Busy as busy:
        print(busy.line)
        rules.handoff(paths, "busy", command=a.cmd)
        return 1


def _run(a, paths, anki_client, calls, learner_core, checks) -> int:
    if a.cmd == "consult":
        # The specialist's opinion for another agent: synchronous, recorded in the vault,
        # never a failure for the caller -- an answer it couldn't give is said in the result.
        from . import consult
        result = consult.consult(paths, " ".join(a.question), a.module, calls=calls)
        if consult.answered(result):
            try:
                rec = consult.record(paths, result, say=lambda s: print(s, file=sys.stderr))
                don.commit(paths, rec["touched"], "consult", say=lambda s: print(s, file=sys.stderr))
            except Exception as exc:  # noqa: BLE001 -- the opinion stands even when the vault write doesn't
                print(CONFIG["say"]["not_committed"].format(why=exc), file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False) if a.json else consult.render(result))
        return 0 if consult.answered(result) else 1
    if a.cmd in CHAT_COMMANDS:
        # The chat tools, from the command line: the same functions Denisse calls,
        # a Claude job waited for rather than handed to the tray.
        from . import chat
        chat.PATHS, chat.BACKGROUND = paths, False
        if calls:
            chat.CALLS = calls
        text = " ".join(getattr(a, "text", []) or [])
        fn = {"explain": lambda: chat.explain(text, a.module), "quiz": lambda: chat.quiz(a.module, text),
              "weak": lambda: chat.weak_topics(a.module), "exam": lambda: chat.exam_question(a.module, text),
              "mark": lambda: chat.mark_answer(text, a.module)}[a.cmd]
        print(fn())
        return 0
    if a.cmd == "ingest":
        moved = [] if a.dry else don.migrate(paths)
        if moved:
            don.commit(paths, moved, "migrate")
        out = ingest.run(paths, call=(calls or {}).get("local"), apply=not a.dry)
        don.commit(paths, out["touched"], "ingest")
        if not a.dry:
            rules.handoff(paths, "ingest", notes=len(out["notes"]), incomplete=len(out["incomplete"]),
                          unchanged=len(out["unchanged"]), failed=len(out["failed"]))
        return 1 if out["failed"] else 0
    if a.cmd == "topics":
        module = _module(paths, a.module)
        if module is None:
            return 1
        topic_map = topics.load(paths, module)
        if topic_map is None:
            print(CONFIG["say"]["no_topics"].format(module=module.title))
            return 1
        print(json.dumps(topic_map, indent=2, ensure_ascii=False))
        return 0
    moved = don.migrate(paths)
    if moved:
        don.commit(paths, moved, "migrate")
    if a.cmd == "cards":
        module = _module(paths, a.module)
        if module is None:
            return 1
        rules.check_kinds()
        out = cards.run(paths, module, calls=calls, checks=checks)
        synced, touched = {}, list(out["touched"])
        if rules.on("sync_after_cards") and out["cards"]:
            synced = _sync(paths, [module], anki_client, learner_core, commit=False)
            touched += [f for f in synced["touched"] if f not in touched]
        don.commit(paths, touched, "cards", module)  # one job, one commit
        rules.handoff(paths, "cards", module=module.title, cards=len(out["cards"]), made=len(out["made"]),
                      held=len(out["held"]), synced=_anki_word(module.slug, synced) if synced
                      else CONFIG["rules"]["handoff"]["not_synced"])
        return 1 if out["failed"] and not out["cards"] else 0
    modules = None
    if a.module:
        module = _module(paths, a.module)
        if module is None:
            return 1
        modules = [module]
    if a.cmd == "progress":
        out = progress.run(paths, modules, learner_core=learner_core)
        don.commit(paths, out["touched"], "progress")
        rules.handoff(paths, "progress", n=len(out["records"]))
        return 0
    out = _sync(paths, modules, anki_client, learner_core)
    h = CONFIG["rules"]["handoff"]
    rules.handoff(paths, "anki-sync",
                  modules=CONFIG["note"]["list_sep"].join(m.title for m in modules) if modules else h["all_modules"],
                  added=sum(v["added"] for v in out["synced"].values()),
                  updated=sum(v["updated"] for v in out["synced"].values()),
                  suspended=sum(v["suspended"] for v in out["scoped"].values()),
                  unsuspended=sum(v["unsuspended"] for v in out["scoped"].values()),
                  synced=h["anki_off"] if not CONFIG["anki"]["connect"]["enabled"]
                  else h["synced"] if out["reachable"] else h["unsynced"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
