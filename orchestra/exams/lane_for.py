"""Which Orchestra lane should a task go to, by the kind of work in it?

    lane_for("code-edit")            -> "codex" if codex has the best working score, else ...
    lane_for(None, "Fix the tray restart bug")   (kind guessed from the title)

The kind is an exam category (code-edit, debug, writing, study, extract) or one of the
agent hub's route kinds (code_change, debug, summarise, ...), mapped to a category below.
No scores, or no working agent among the lanes: the old default, "claude".
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import core
from .settings import load

DEFAULT_LANE = load()["routing"]["default_lane"]


def orchestra_lanes(config: Path | None = None) -> tuple[str, ...]:
    """The lanes the runner knows: the keys of config.json "lane_argv" (claude, codex, gemini,
    copilot, ...). A lane with no passing scores is never picked, so a new lane (copilot, 3 Nov
    2026) only gets tasks that name it (Lane: copilot) until the daily exams have scored it."""
    path = config or core.REPO / load()["paths"]["orchestra_config"]
    try:
        argv = json.loads(path.read_text(encoding="utf-8")).get("lane_argv") or {}
    except (OSError, ValueError, AttributeError):
        argv = {}
    return tuple(argv) if isinstance(argv, dict) and argv else (load()["routing"]["default_lane"],)


def category_for(kind: str | None, title: str = "") -> str | None:
    policy = load()
    if kind:
        k = kind.strip().lower()
        if k in policy["categories"]:
            return k
        return policy["routing"]["kind_to_category"].get(k.replace("-", "_"))
    t = title.lower()
    for pat, cat in policy["routing"]["title_routes"]:
        if re.search(pat, t):
            return cat
    return None


def lane_for(kind: str | None, title: str = "", scores_file: Path | None = None,
             lanes: tuple[str, ...] | None = None) -> str:
    default_lane = load()["routing"]["default_lane"]
    cat = category_for(kind, title)
    if not cat:
        return default_lane
    scores = core.load_scores(scores_file)
    if not scores["agents"]:
        return default_lane
    b = core.best(scores, cat, among=list(lanes or orchestra_lanes()))
    return b["agent"] if b else default_lane
