"""atrium/config.json: every limit, path, name and sentence atrium/ uses (AGENTS.md Rule 0).

    conf.load()["capture"]["every_tokens"]

ATRIUM_CONFIG points at another copy (tests do). Keys starting with "_" are notes.
Read fresh on every call, so a changed file takes effect on the next run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT = Path(__file__).with_name("config.json")


def path() -> Path:
    return Path(os.environ.get("ATRIUM_CONFIG") or DEFAULT)


def load() -> dict:
    with open(path(), encoding="utf-8-sig") as f:
        return json.load(f)


def section(name: str) -> dict:
    return load()[name]
