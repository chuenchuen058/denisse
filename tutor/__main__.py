"""python -m tutor <command>

  ingest [--dry]          read the KEATS inbox folders, write tutor notes and topic maps
  topics MODULE           print a module's topic map (name, code or alias words)
  cards MODULE            make cards from every vault note of a module (changed notes only)
  anki-sync [MODULE]      write the Anki TSV per module; with AnkiConnect, add/update the
                          cards and read the review stats (weak topics) back

Every command takes --vault PATH and --state DIR (the old private state
folder, read until its files are moved into the vault). Every vault write is
committed in the vault's own git repo, only the files written.
--dry scans and matches only: no model call, nothing written.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import anki, cards, conf, don, ingest, topics
from .conf import CONFIG


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


def main(argv=None, anki_client=None, calls=None, learner_core=None, checks=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tutor", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("ingest", "topics", "cards", "anki-sync"):
        s = sub.add_parser(name)
        s.add_argument("--vault", default=None)
        s.add_argument("--state", default=None)
        if name == "ingest":
            s.add_argument("--dry", action="store_true")
        elif name == "anki-sync":
            s.add_argument("module", nargs="?", default=None)
        else:
            s.add_argument("module")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    paths = _paths(a)
    if a.cmd == "ingest":
        moved = [] if a.dry else don.migrate(paths)
        if moved:
            don.commit(paths, moved, "migrate")
        out = ingest.run(paths, call=(calls or {}).get("local"), apply=not a.dry)
        don.commit(paths, out["touched"], "ingest")
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
        out = cards.run(paths, module, calls=calls, checks=checks)
        don.commit(paths, out["touched"], "cards", module)
        return 1 if out["failed"] and not out["cards"] else 0
    modules = None
    if a.module:
        module = _module(paths, a.module)
        if module is None:
            return 1
        modules = [module]
    out = anki.run(paths, modules, client=anki_client, learner_core=learner_core)
    don.commit(paths, out["touched"], "anki")
    return 0


if __name__ == "__main__":
    sys.exit(main())
