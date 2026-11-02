"""Agent exams: measure which agent is good at what, and route by it.

    python -m orchestra.exams run [--agent X] [--category Y] [--include-claude]
    python -m orchestra.exams canary [--agent X] [--include-claude]   # one 30 s exam per agent, daily
    python -m orchestra.exams best <category-or-hub-kind> [--among a,b] [--json]
    python -m orchestra.exams report                                  # print the table, rewrite the vault note

Scores: .review/exams/scores.json (last 20 results per agent x category).
Vault note: <vault_dir>/Dev/Agent-Scores.md, vault_dir from orchestra/config.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import core
from .lane_for import category_for


def _agents(a) -> dict:
    from . import agents
    found = agents.available(include_claude=a.include_claude, only=a.agent)
    if a.agent and not found:
        sys.exit(f"no agent {a.agent!r} (local models need Ollama running; claude needs --include-claude)")
    return found


def _finish(scores: dict, a) -> None:
    note = core.write_note(scores, Path(a.vault) if a.vault else None)
    print(f"scores: {a.scores or core.scores_path()}" + (f"; note: {note}" if note else "; no vault, note not written"))
    down = core.not_working(scores)
    if down:
        print("Not working: " + "; ".join(f"{k} ({v})" for k, v in sorted(down.items())))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m orchestra.exams", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scores", help="scores.json path (default .review/exams/scores.json)")
    ap.add_argument("--vault", help="vault folder for the note (default: config.json vault_dir)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "canary"):
        p = sub.add_parser(name)
        p.add_argument("--agent", help="codex, claude, copilot, gemini, local, or local:<model>")
        p.add_argument("--include-claude", action="store_true", help="also examine claude (uses Chuen's allowance)")
        p.add_argument("--timeout", type=float, help="per-exam timeout, seconds")
        if name == "run":
            p.add_argument("--category", choices=core.CATEGORIES)
    p = sub.add_parser("best")
    p.add_argument("category", help="exam category, or an agent hub route kind (code_change, summarise, ...)")
    p.add_argument("--among", help="comma-separated agents to choose from")
    p.add_argument("--json", action="store_true")
    sub.add_parser("report")
    a = ap.parse_args(argv)
    scores_file = Path(a.scores) if a.scores else None

    if a.cmd == "best":
        cat = category_for(a.category)
        scores = core.load_scores(scores_file)
        b = core.best(scores, cat, among=a.among.split(",") if a.among else None) if cat else None
        if a.json:
            print(json.dumps(b or {"agent": None, "category": cat}))
        else:
            print(b["agent"] if b else f"none (no working agent has passed a {cat or a.category!r} exam)")
        return 0 if b else 1
    if a.cmd == "report":
        scores = core.load_scores(scores_file)
        print(core.render_note(scores))
        _finish(scores, a)
        return 0
    if a.cmd == "run":
        exams = core.load_bank(a.category)
        scores = core.run(_agents(a), exams, scores_file=scores_file, timeout=a.timeout)
    else:  # canary
        exams = [e for e in core.load_bank() if e.get("canary")][:1]
        scores = core.run(_agents(a), exams, scores_file=scores_file, timeout=a.timeout or 30)
    _finish(scores, a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
