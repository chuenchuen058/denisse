"""Check that every decision line says who made it (Thomas's `brain.validate credits`).

Decision notes: Atlas/Settled.md, Atlas/Standing-Asks.md, Atlas/Decisions-Log.md,
and Calendar/Handoff-*.md changed in the last `days` days. A decision line is a
bullet starting with a date or a time. It passes if it carries a credit tag:
[Chuen], [Claude], [Codex], [Gemini], [Copilot], [Denisse], [measured],
or [Claude proposed, Chuen approved].
"""
from __future__ import annotations

import datetime as dt
import re
import time

from .context import VAULT

CREDIT = re.compile(r"\[(Chuen|Claude|Codex|Gemini|Copilot|Denisse|measured|claude|codex|gemini|copilot|denisse"
                    r"|Claude proposed, Chuen approved)[^\]]*\]")
DECISION = re.compile(r"^\s*[-*]\s+(\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2})\b")
NOTES = ("Atlas/Settled.md", "Atlas/Standing-Asks.md", "Atlas/Decisions-Log.md")


def check(days: int = 7) -> list[tuple[str, int, str]]:
    files = [VAULT / n for n in NOTES]
    cutoff = time.time() - days * 86400
    files += [p for p in (VAULT / "Calendar").glob("Handoff-*.md") if p.stat().st_mtime >= cutoff] \
        if (VAULT / "Calendar").is_dir() else []
    bad = []
    for p in files:
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if DECISION.match(line) and not CREDIT.search(line):
                bad.append((p.relative_to(VAULT).as_posix(), i, line.strip()[:120]))
    return bad
