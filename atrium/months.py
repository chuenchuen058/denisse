"""Month folders for machine-written logs (atrium/config.json "months").

Calendar/Chats and Calendar/Sessions keep one subfolder per month (vault tidy,
15 Nov 2026). New notes go into the month folder; lookups ("is this already
there?") search the base folder and every month folder, so a note written
before the tidy, or moved by it, is still found.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from . import conf


def _c() -> dict:
    return conf.section("months")


def folder(date: str) -> str:
    """'2026-11-15' -> the month folder name (shipped pattern: '2026-11')."""
    return dt.date.fromisoformat(date[:10]).strftime(_c()["folder"])


def name_date(name: str) -> str | None:
    """The YYYY-MM-DD a log's file name starts with, or None."""
    m = re.match(_c()["dated_name"], name)
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1)).isoformat()
    except ValueError:
        return None


def subfolders(base: Path) -> list[Path]:
    """The base folder itself, then each direct subfolder (the month folders)."""
    if not base.is_dir():
        return []
    return [base, *sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))]


def find(base: Path, name: str) -> Path | None:
    """An existing note called `name` in the base folder or any month folder."""
    for d in subfolders(base):
        p = d / name
        if p.is_file():
            return p
    return None


def names(base: Path) -> set[str]:
    """Casefolded file names across the base folder and its month folders."""
    return {p.name.casefold() for d in subfolders(base) for p in d.iterdir() if p.is_file()}


def notes(base: Path, pattern: str = "*.md") -> list[Path]:
    """Notes in the base folder and every month folder."""
    return sorted(p for d in subfolders(base) for p in d.glob(pattern) if p.is_file())
