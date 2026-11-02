"""Which Orchestra lane should a task go to, by the kind of work in it?

    lane_for("code-edit")            -> "codex" if codex has the best working score, else ...
    lane_for(None, "Fix the tray restart bug")   (kind guessed from the title)

The kind is an exam category (code-edit, debug, writing, study, extract) or one of the
agent hub's route kinds (code_change, debug, summarise, ...), mapped to a category below.
No scores, or no working agent among the lanes: the old default, "claude".
"""
from __future__ import annotations

import re
from pathlib import Path

from . import core

DEFAULT_LANE = "claude"
LANES = ("claude", "codex", "gemini")  # the Orchestra lanes runner.py knows

# agent hub route kinds (server.mjs ROUTES) -> exam category. Kinds with no fitting exam
# (github, shell_help) are left out: those keep the hub's fixed rules.
KIND_TO_CATEGORY = {
    "code_change": "code-edit", "quick_code": "code-edit", "fix": "code-edit", "code": "code-edit",
    "debug": "debug", "code_review": "debug",
    "summarise": "writing", "research": "writing", "long_read": "writing", "write": "writing",
    "study": "study", "maths": "study", "math": "study",
    "extract": "extract",
}

GUESS = [
    (r"\b(fix|implement|refactor|add (a )?test|rename|edit|code)\b", "code-edit"),
    (r"\b(bug|error|traceback|crash|failing|debug)\b", "debug"),
    (r"\b(fourier|signal|maths?|equation|exam|lecture|revision|study)\b", "study"),
    (r"\b(extract|parse|pull .* into|json|csv)\b", "extract"),
    (r"\b(write|rewrite|summari[sz]e|draft|email|essay|notes?)\b", "writing"),
]


def category_for(kind: str | None, title: str = "") -> str | None:
    if kind:
        k = kind.strip().lower()
        if k in core.CATEGORIES:
            return k
        return KIND_TO_CATEGORY.get(k.replace("-", "_"))
    t = title.lower()
    for pat, cat in GUESS:
        if re.search(pat, t):
            return cat
    return None


def lane_for(kind: str | None, title: str = "", scores_file: Path | None = None) -> str:
    cat = category_for(kind, title)
    if not cat:
        return DEFAULT_LANE
    scores = core.load_scores(scores_file)
    if not scores["agents"]:
        return DEFAULT_LANE
    b = core.best(scores, cat, among=list(LANES))
    return b["agent"] if b else DEFAULT_LANE
