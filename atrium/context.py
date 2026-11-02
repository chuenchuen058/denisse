"""One context pack for every agent (after Thomas's `atrium-context show --lane`).

Codex, Gemini and Copilot can't be trusted to go and read the vault, so the
agent hub hands them this pack at the top of every task instead. It holds the
live, shared state, trimmed to a budget so it never swamps the task:

  1. Standing asks (Chuen's rules)        Atlas/Standing-Asks.md
  2. Before planning anything about him   Atlas/Concepts/Learning/Learner-Model.md
  3. Active right now / Waiting on Chuen  hot.md
  4. STATE NOW (what's running)           STATE NOW.md
  5. Settled (latest lines)               Atlas/Settled.md
  6. Today's handoff (latest lines)       Calendar/Handoff-<today>.md

Missing notes are skipped. Read-only.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

VAULT = Path(os.environ.get("DENISSE_VAULT",
                            Path.home() / "OneDrive - King's College London" / "Denisse"))
LANES = ("claude", "codex", "gemini", "copilot", "denisse")
BUDGET = {"claude": 6000, "codex": 4000, "gemini": 8000, "copilot": 2500, "denisse": 2500}


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


def parts() -> list[tuple[str, str]]:
    hot = _read("hot.md")
    today = dt.date.today().isoformat()
    learner = _section(_read("Atlas/Concepts/Learning/Learner-Model.md"), "Before planning anything about Chuen")
    kept, pending = [], None
    for l in learner.splitlines():  # keep only areas with something known
        if not l.strip() or "- unknown" in l:
            continue
        if l.startswith("#"):
            pending = l
            continue
        if pending:
            kept.append(pending); pending = None
        kept.append(l)
    learner = "\n".join(kept)
    return [
        ("Standing asks (Chuen's rules)", _bullets(_body(_read("Atlas/Standing-Asks.md")))),
        ("Before planning anything about Chuen", learner),
        ("Active right now", _bullets(_section(hot, "Active right now"))),
        ("Waiting on Chuen", _bullets(_section(hot, "Waiting on Chuen"))),
        ("STATE NOW", re.sub(r"\A## STATE NOW\s*\n", "", _body(_read("STATE NOW.md")))),
        ("Settled (latest)", _bullets(_body(_read("Atlas/Settled.md")), last=12)),
        (f"Today's handoff ({today})", _bullets(_read(f"Calendar/Handoff-{today}.md"), last=15)),
    ]


def show(lane: str = "claude") -> str:
    budget = BUDGET.get(lane, 4000)
    out = [f"# Shared context for {lane} (from Chuen's vault, {dt.datetime.now():%Y-%m-%d %H:%M})",
           "Read-only. Facts here outrank your assumptions; anything missing is unknown."]
    used = sum(len(x) for x in out)
    for title, text in parts():
        if not text.strip():
            continue
        block = f"\n## {title}\n{text.strip()}"
        if used + len(block) > budget:
            room = budget - used - 40
            if room < 200:
                out.append(f"\n## {title}\n(cut for length; see the vault)")
                continue
            block = block[:room].rsplit("\n", 1)[0] + "\n(... cut for length)"
        out.append(block)
        used += len(block)
    return "\n".join(out) + "\n"
