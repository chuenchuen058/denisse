"""Append tagged lines to today's handoff note, the shared log every agent and
the nightly review read (path, line shape and new-note text: atrium/config.json
"handoff"; shipped: <vault>/Calendar/Handoff-YYYY-MM-DD.md, "- HH:MM [agent] text",
the same format as the agent hub's remember()).
Append-only; never edits other notes; silently does nothing without a vault."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from . import conf
from .context import VAULT


def path(date: str | None = None, vault: Path | None = None):
    return (Path(vault) if vault else VAULT) / conf.section("handoff")["note"].format(
        date=date or dt.date.today().isoformat())


def remember(agent: str, lines: list[str], vault: Path | None = None, now: dt.datetime | None = None) -> bool:
    c = conf.section("handoff")
    lines = [re.sub(r"\s+", " ", l).strip()[:c["line_chars"]] for l in lines if l and l.strip()]
    vault = Path(vault) if vault else VAULT
    if not lines or not vault.is_dir():
        return False
    now = now or dt.datetime.now()
    d = now.date().isoformat()
    p = path(d, vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(c["new_note"].format(date=d), encoding="utf-8")
    with p.open("a", encoding="utf-8") as f:
        for l in lines:
            f.write(c["line"].format(time=now.strftime(c["time_format"]), agent=agent, text=l) + "\n")
    return True
