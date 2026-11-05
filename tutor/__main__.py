"""python -m tutor <command>

  ingest [--dry] [--vault PATH] [--state DIR]   read the KEATS inbox folders, write tutor notes and topic maps
  topics MODULE [--vault PATH] [--state DIR]    print a module's topic map (name, code or alias words)

--dry scans and matches only: no model call, nothing written.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import conf, ingest, topics
from .conf import CONFIG


def _paths(a) -> conf.Paths:
    p = conf.paths() if not (a.vault and a.state) else None
    vault = a.vault or p.vault
    state = a.state or p.state
    shared = p.shared_inboxes if p else []
    return conf.Paths(vault=vault, state=state, shared_inboxes=shared)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tutor", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("ingest", "topics"):
        s = sub.add_parser(name)
        s.add_argument("--vault", default=None)
        s.add_argument("--state", default=None)
        if name == "ingest":
            s.add_argument("--dry", action="store_true")
        else:
            s.add_argument("module")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    paths = _paths(a)
    if a.cmd == "ingest":
        out = ingest.run(paths, apply=not a.dry)
        return 1 if out["failed"] else 0
    modules = conf.modules(paths)
    module = conf.find_module(a.module, modules)
    if module is None:
        print(CONFIG["say"]["no_module"].format(text=a.module, modules=", ".join(m.title for m in modules) or "-"))
        return 1
    topic_map = topics.load(paths, module)
    if topic_map is None:
        print(CONFIG["say"]["no_topics"].format(module=module.title))
        return 1
    print(json.dumps(topic_map, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
