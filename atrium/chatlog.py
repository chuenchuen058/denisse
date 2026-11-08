"""Append a section to the day's chat note in the vault, add one handoff line, and
commit only those two files (the writing path of .review/hub_digest.py, shared).

    save("hub", body)                  # the hub chat's digest (python -m atrium hub-save <md>)
    save("atrium", body, since=..., …)  # the laptop digest (python -m atrium digest)

Each writer has its own heading (atrium/config.json "chatlog" -> "writers"), and the
note is only ever appended to, so one writer never clobbers another's entries.
Never writes anything under a never_write folder (shipped: .obsidian). A vault that
isn't a git repo, or a failed commit, leaves the notes written and says so.
"""
from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

from . import conf, handoff


def _c() -> dict:
    return conf.section("chatlog")


def note_path(vault: Path, date: str) -> Path:
    return vault / _c()["note"].format(date=date)


def _guard(vault: Path, p: Path) -> None:
    parts = p.relative_to(vault).parts
    bad = set(_c()["never_write"]) & set(parts)
    if bad:
        raise ValueError(f"refusing to write under {sorted(bad)}: {p}")


def _append(p: Path, text: str) -> None:
    """Append, starting on a fresh line after one blank line."""
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    trailing = len(old) - len(old.rstrip("\n"))
    lead = "\n" * max(0, 2 - trailing) if old else ""
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(lead + text)


def save(writer: str, body: str, vault: Path | None = None, now: dt.datetime | None = None,
         commit: bool = True, **fields) -> dict:
    """Write `body` under the writer's heading. Returns {"note", "files", "committed", "error"}."""
    c = _c()
    w = c["writers"][writer]
    vault = Path(vault) if vault else handoff.VAULT
    now = now or dt.datetime.now()
    date, time = now.date().isoformat(), now.strftime(c["time_format"])
    kw = dict(fields, date=date, time=time)
    note = note_path(vault, date)
    _guard(vault, note)
    note.parent.mkdir(parents=True, exist_ok=True)
    if not note.exists():
        note.write_text(c["new_note"].format(date=date), encoding="utf-8", newline="\n")
    _append(note, w["heading"].format(**kw) + "\n\n" + body.strip() + "\n")
    kw["stem"] = note.stem
    hp = handoff.path(date, vault=vault)
    _guard(vault, hp)
    handoff.remember(w["agent"], [w["handoff"].format(**kw)], vault=vault, now=now)
    files = [str(p.relative_to(vault)) for p in (note, hp) if p.exists()]
    out = {"note": note, "files": files, "committed": False, "error": ""}
    if not commit:
        return out
    git = list(c["git"])
    try:
        subprocess.run([*git, "add", "--", *files], cwd=vault, check=True, capture_output=True,
                       text=True, timeout=c["git_timeout_s"])
        subprocess.run([*git, "commit", "-q", "-m", w["commit"].format(**kw), "--", *files], cwd=vault,
                       check=True, capture_output=True, text=True, timeout=c["git_timeout_s"])
        out["committed"] = True
    except (OSError, subprocess.SubprocessError) as exc:
        out["error"] = (getattr(exc, "stderr", "") or str(exc)).strip()
    return out
