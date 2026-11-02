"""Append tagged lines to today's handoff note, the shared log every agent and
the nightly review read: <vault>/Calendar/Handoff-YYYY-MM-DD.md.
Same format as the agent hub's remember(): "- HH:MM [agent] text".
Append-only; never edits other notes; silently does nothing without a vault."""
from __future__ import annotations

import datetime as dt
import re

from .context import VAULT


def path(date: str | None = None):
    return VAULT / "Calendar" / f"Handoff-{date or dt.date.today().isoformat()}.md"


def remember(agent: str, lines: list[str]) -> bool:
    lines = [re.sub(r"\s+", " ", l).strip()[:400] for l in lines if l and l.strip()]
    if not lines or not VAULT.is_dir():
        return False
    now = dt.datetime.now()
    p = path(now.date().isoformat())
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        d = now.date().isoformat()
        p.write_text(f"---\nname: Handoff-{d}\ndescription: What every agent did on {d}, one line each, tagged with who.\n"
                     f"updated: {d}\ntype: dated\nstatus: active\ntags: [handoff]\nprotection: append\n---\n\n"
                     f"# Handoff {d}\n\nOne line per decision, correction, hand-off or thing waiting on Chuen, "
                     f"tagged with who wrote it.\n\n## Log\n", encoding="utf-8")
    with p.open("a", encoding="utf-8") as f:
        for l in lines:
            f.write(f"- {now:%H:%M} [{agent}] {l}\n")
    return True
