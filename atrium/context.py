"""One context pack for every agent (after Thomas's `atrium-context show --lane`).

Codex, Gemini and Copilot can't be trusted to go and read the vault, so the
agent hub hands them this pack at the top of every task instead. It holds the
live, shared state, trimmed to a per-lane budget so it never swamps the task.
Which notes, sections and line counts go in, in what order, and every budget
live in atrium/config.json ("context"); with the shipped data the pack is:
standing asks, the learner model's known areas, hot.md's active and waiting
lists, STATE NOW, the latest settled lines and today's handoff.

Missing notes are skipped. Read-only.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path

from . import conf

REPO = Path(__file__).resolve().parent.parent


def _vault() -> Path:
    """DENISSE_VAULT, else settings.vault() (config.json / Obsidian's own registry),
    else the fallback in atrium/config.json under the home folder."""
    if os.environ.get("DENISSE_VAULT"):
        return Path(os.environ["DENISSE_VAULT"])
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        import settings
        return Path(settings.vault())
    except Exception:
        return Path.home() / conf.section("vault")["fallback"]


VAULT = _vault()
_C = conf.section("context")
LANES = tuple(_C["lanes"])
BUDGET = dict(_C["budget"])


def _read(rel: str) -> str:
    try:
        return (VAULT / rel).read_text(encoding="utf-8")
    except OSError:
        return ""


def _body(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip()


def _section(text: str, heading: str) -> str:
    m = re.search(rf"^##+ {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def _bullets(text: str, last: int | None = None) -> str:
    lines = [l for l in text.splitlines() if l.lstrip().startswith(("- ", "* ", "|"))]
    lines = [l for l in lines if not re.match(r"^\|\s*-", l)]
    return "\n".join(lines[-last:] if last else lines)


def _known_only(text: str, unknown: str) -> str:
    """Drop the areas (### headings) whose every line is still unknown."""
    kept, pending = [], None
    for l in text.splitlines():
        if not l.strip() or unknown in l:
            continue
        if l.startswith("#"):
            pending = l
            continue
        if pending:
            kept.append(pending); pending = None
        kept.append(l)
    return "\n".join(kept)


def parts() -> list[tuple[str, str]]:
    c = conf.section("context")
    today = dt.date.today().isoformat()
    out = []
    for p in c["parts"]:
        text = _read(p["note"].format(today=today))
        if p.get("body"):
            text = _body(text)
        if p.get("section"):
            text = _section(text, p["section"])
        if p.get("strip_heading"):
            text = re.sub(rf"\A## {re.escape(p['strip_heading'])}\s*\n", "", text)
        if p.get("known_only"):
            text = _known_only(text, c["unknown_marker"])
        if p.get("bullets"):
            text = _bullets(text, last=p.get("last"))
        out.append((p["title"].format(today=today), text))
    return out


def show(lane: str | None = None) -> str:
    c = conf.section("context")
    lane = lane or c["default_lane"]
    budget = BUDGET.get(lane, c["default_budget"])
    out = [c["header"].format(lane=lane, now=dt.datetime.now().strftime(c["header_time"])), c["preamble"]]
    used = sum(len(x) for x in out)
    for title, text in parts():
        if not text.strip():
            continue
        block = f"\n## {title}\n{text.strip()}"
        if used + len(block) > budget:
            room = budget - used - c["cut_margin"]
            if room < c["cut_min_room"]:
                out.append(f"\n## {title}\n{c['cut_whole']}")
                continue
            block = block[:room].rsplit("\n", 1)[0] + "\n" + c["cut_tail"]
        out.append(block)
        used += len(block)
    return "\n".join(out) + "\n"
