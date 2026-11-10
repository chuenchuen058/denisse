"""Per-job local model routing, read from model_routing.json (AGENTS.md Rule 0).

    model_routing.model_for("don_cards", entry="chat")                  # a job on config.json's chat model
    model_routing.model_for("orchestra_local_team", cfg["local_team_model"])  # a job with its own default

A job whose "model" is null keeps today's model: `default`, or the config.json
models entry `entry` (whose env override, DENISSE_MODEL for chat, still wins as
it always did). `python -m bench run` sets override_env for the model it is
measuring; that wins over everything. `python -m bench pick` only proposes
edits to the JSON. MODEL_ROUTING points at another file (tests).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def path() -> Path:
    return Path(os.environ.get("MODEL_ROUTING") or Path(__file__).with_suffix(".json"))


def load() -> dict:
    """The whole file; {} when it is missing or unreadable (every job keeps today's model)."""
    try:
        return json.loads(path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def jobs() -> dict:
    return {k: v for k, v in (load().get("jobs") or {}).items() if not k.startswith("_")}


def override_env() -> str | None:
    return load().get("override_env")


def routed(job: str | None) -> str | None:
    """The model config pins for `job`, or None for today's."""
    if not job:
        return None
    return (jobs().get(job) or {}).get("model") or None


def model_for(job: str | None, default: str | None = None, *, entry: str | None = None) -> str | None:
    env = override_env()
    if env and os.environ.get(env):
        return os.environ[env]
    if entry is not None:
        import model_config
        own = model_config.entry(entry).get("env")
        if own and os.environ.get(own):
            return os.environ[own]
        return routed(job) or model_config.model(entry)
    return routed(job) or default
