"""atrium/config.json: every limit, path, name and sentence atrium/ uses (AGENTS.md Rule 0).

    conf.load()["capture"]["every_tokens"]

ATRIUM_CONFIG points at another copy (tests do). Keys starting with "_" are notes.
Read fresh on every call, so a changed file takes effect on the next run, except inside
`with conf.held():` (one live page build), where it is read once for that thread and every
call shares that copy: the build asks for a section ~1500 times, and parsing the file each
time was two thirds of the page's build time (15 Nov 2026). Treat what it returns as read-only.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path

DEFAULT = Path(__file__).with_name("config.json")
_held = threading.local()


def path() -> Path:
    return Path(os.environ.get("ATRIUM_CONFIG") or DEFAULT)


def _read() -> dict:
    with open(path(), encoding="utf-8-sig") as f:
        return json.load(f)


def load() -> dict:
    data = getattr(_held, "data", None)
    return data if data is not None else _read()


def section(name: str) -> dict:
    return load()[name]


@contextlib.contextmanager
def held():
    """Read the config once for everything this thread does inside (nested: the outer read)."""
    if getattr(_held, "data", None) is not None:
        yield
        return
    _held.data = _read()
    try:
        yield
    finally:
        _held.data = None
