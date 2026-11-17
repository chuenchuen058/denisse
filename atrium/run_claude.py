"""Click a box on the live Atrium page and it runs as a prompt in Claude (Chuen, 17 Nov 2026).

The page sends what was clicked (a hot.md bullet with its group, or an Orchestra task's id and
title); this builds the prompt from the config's template and opens Claude Code in a new window
on the laptop with that prompt, so Chuen sees it and can carry on the conversation there.

Only the live page's own server calls launch(): atrium/live.py checks the loopback Host, the
same-origin Origin and the JSON body like its other writes, and also the per-run token below,
which only a page that could read /data knows. Every word, path, limit and argv:
atrium/config.json "live" -> "board" -> "run".
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from . import conf

HERE = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(24)  # new each time the server starts; sent only inside /data


def cfg() -> dict | None:
    """The "run" section, or None when it is missing or switched off."""
    c = conf.section("live")["board"].get("run")
    return c if c and c.get("on", True) else None


def page_cfg() -> dict | None:
    """What app.js needs: the path, the token and the words. None: no Run on this page."""
    c = cfg()
    if not c:
        return None
    return {"http_path": c["http_path"], "token": TOKEN, "words": c["words"]}


def _clean(s, limit: int) -> str:
    """One line of plain text, at most limit characters."""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:limit]


def build_prompt(body: dict, c: dict | None = None) -> str:
    """The prompt for what was clicked. Raises ValueError for anything the page can't have sent."""
    c = c or cfg()
    if not c:
        raise ValueError("Run in Claude is off")
    kind = body.get("kind")
    tmpl = c["prompts"].get(kind) if isinstance(kind, str) else None
    if not tmpl:
        raise ValueError(f"unknown kind {kind!r}")
    lim = int(c["field_chars"])
    fields = {k: _clean(body.get(k), lim) for k in c["fields"][kind]}
    if not fields.get(c["required"][kind]):
        raise ValueError(f"{c['required'][kind]} is empty")
    idre = c.get("id_pattern")
    if kind == "task" and idre and not re.fullmatch(idre, fields.get("id", "")):
        raise ValueError("not a task id")
    return tmpl.format(**fields)[: int(c["max_chars"])]


def _claude(c: dict) -> str:
    """The Claude Code executable: the config's name on PATH, else the first fallback that exists."""
    found = shutil.which(c["claude"])
    if found:
        return found
    for f in c.get("claude_fallbacks", []):
        p = Path(os.path.expandvars(os.path.expanduser(f)))
        if p.is_file():
            return str(p)
    raise FileNotFoundError(f"{c['claude']} is not on PATH")


def argv_for(prompt: str, c: dict) -> list[str]:
    exe = _claude(c)
    return [a.replace("{claude}", exe).replace("{prompt}", prompt) if a in ("{claude}", "{prompt}") else a
            for a in c["argv"]]


def launch(body: dict, popen=subprocess.Popen) -> dict:
    """Open Claude Code with the prompt for body, in a new window. Returns {"ok", "prompt"}."""
    c = cfg()
    if not c:
        raise ValueError("Run in Claude is off")
    if not secrets.compare_digest(str(body.get("token", "")), TOKEN):
        raise PermissionError("bad token")
    prompt = build_prompt(body, c)
    cwd = (HERE.parent / c["cwd"]).resolve() if c.get("cwd") else HERE.parent
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if c.get("new_console", True) else 0
    popen(argv_for(prompt, c), cwd=str(cwd), creationflags=flags)
    return {"ok": True, "prompt": prompt}
