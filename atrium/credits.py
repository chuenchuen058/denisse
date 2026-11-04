"""Check that every decision line says who made it (Thomas's `brain.validate credits`).

Decision notes and the credit tags that count live in atrium/config.json
("credits"; shipped: Atlas/Settled.md, Atlas/Standing-Asks.md,
Atlas/Decisions-Log.md, plus Calendar/Handoff-*.md changed in the last `days`
days). A decision line is a bullet starting with a date or a time. It passes if
it carries a credit tag: [<tag>] or [<tag> ...] for any configured tag.
"""
from __future__ import annotations

import re
import time

from . import conf
from .context import VAULT

# The shape of a decision line is the vault's fixed note format, not a guess at wording.
DECISION = re.compile(r"^\s*[-*]\s+(\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2})\b")


def credit_pattern(tags: list[str]) -> re.Pattern:
    return re.compile(r"\[(" + "|".join(re.escape(t) for t in tags) + r")[^\]]*\]")


def check(days: int | None = None) -> list[tuple[str, int, str]]:
    c = conf.section("credits")
    days = c["default_days"] if days is None else days
    credit = credit_pattern(c["tags"])
    files = [VAULT / n for n in c["notes"]]
    cutoff = time.time() - days * 86400
    recent = VAULT / c["recent_glob_dir"]
    files += [p for p in recent.glob(c["recent_glob"]) if p.stat().st_mtime >= cutoff] if recent.is_dir() else []
    bad = []
    for p in files:
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if DECISION.match(line) and not credit.search(line):
                bad.append((p.relative_to(VAULT).as_posix(), i, line.strip()[:c["line_chars"]]))
    return bad
