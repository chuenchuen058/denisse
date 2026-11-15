"""Who may run unattended (audit 10 Nov 2026, O5).

A task whose From: (or Source:) header names a source that is not a person, such as the
autopilot, the agent hub or the local team, runs unattended only on a guarded lane and only
with gate commands of a known shape. Everything else from such a source is a draft for Chuen.
A task with no such header (Chuen's, or a chat's on his instruction) is not touched.

Every list here is config (orchestra/config.json, mirrored in config_defaults.json):
  unattended_source_keys    config keys whose values name a non-human source
  unattended_sources        more such names (sources with no config key of their own)
  unattended_source_headers the headers that name a task's source
  guarded_lanes             lanes that run under the guard hook
  unattended_gate_forbidden characters no such task's gate command may contain
  unattended_gate_patterns  regexes; each gate command must fully match one

new_task.create() checks this for every task it writes; the runner checks it again in
why_not_runnable() for a file written some other way.
"""
from __future__ import annotations

import re

KEYS = ("unattended_source_keys", "unattended_sources", "unattended_source_headers", "guarded_lanes",
        "unattended_gate_forbidden", "unattended_gate_patterns", "unattended_draft_note")


def _norm(s) -> str:
    return " ".join(str(s or "").split()).lower()


def sources(cfg: dict) -> set[str]:
    """Every non-human source name, normalised."""
    out = {_norm(cfg.get(k)) for k in cfg.get("unattended_source_keys") or [] if isinstance(cfg.get(k), str)}
    out |= {_norm(s) for s in cfg.get("unattended_sources") or [] if isinstance(s, str)}
    return {s for s in out if s}


def is_unattended(source, cfg: dict) -> bool:
    """`source` names a non-human source: one of the names, or one followed by more words
    (admission.py writes "Source: local-team (llama3.1:8b; ...)"; "agent hub queue_task" is
    still the hub). Case and spacing don't matter."""
    s = _norm(source)
    if not s:
        return False
    return any(s == name or s.startswith(name + " ") or s.startswith(name + "(") for name in sources(cfg))


def task_source(fields: dict, cfg: dict) -> str | None:
    """The first non-human source named by one of the task's source headers (fields keyed in
    lower case, as the runner parses them), else None."""
    for h in cfg.get("unattended_source_headers") or []:
        value = fields.get(str(h).lower())
        if is_unattended(value, cfg):
            return value
    return None


def gate_problem(cmd: str, cfg: dict) -> str | None:
    """Why this gate command is not allowed for a non-human task, or None."""
    bad = sorted({ch for ch in cfg.get("unattended_gate_forbidden") or [] if ch and ch in cmd})
    if bad:
        return f"gate command {cmd!r} contains {' '.join(repr(b) for b in bad)}"
    for pat in cfg.get("unattended_gate_patterns") or []:
        try:
            if re.fullmatch(pat, cmd):
                return None
        except re.error:
            continue
    return f"gate command {cmd!r} is not of a known shape (config unattended_gate_patterns)"


def problem(source, lane: str | None, gate: list[str] | None, cfg: dict) -> str | None:
    """Why a task from `source` on `lane` with these gate commands may not run unattended, or
    None. A human source (or none) is never a problem."""
    if not is_unattended(source, cfg):
        return None
    guarded = [_norm(l) for l in cfg.get("guarded_lanes") or []]
    if _norm(lane) not in guarded:
        return (f"from {' '.join(str(source).split())}: lane {lane or '?'} is not guarded "
                f"(config guarded_lanes: {', '.join(guarded) or 'none'})")
    cmds = [c.strip() for c in gate or [] if c.strip()]
    if not cmds:
        return f"from {' '.join(str(source).split())}: no gate command"
    for c in cmds:
        why = gate_problem(c, cfg)
        if why:
            return f"from {' '.join(str(source).split())}: {why}"
    return None


def first_guarded(cfg: dict) -> str | None:
    lanes = [l for l in cfg.get("guarded_lanes") or [] if isinstance(l, str) and l.strip()]
    return lanes[0].strip().lower() if lanes else None
