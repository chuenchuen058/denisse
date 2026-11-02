"""Save Claude Code sessions into the vault on their own (after Thomas's
compact-retention and handoff hooks).

Runs as a Claude Code hook (hook JSON on stdin: session_id, transcript_path, cwd,
hook_event_name). A session is captured
  * on Stop, once it has grown by CAPTURE_EVERY_TOKENS (~4 bytes a token) since the
    last capture, so long sessions are saved as they go, not only at the end;
  * always on PreCompact (before Claude Code drops detail) and on SessionEnd.

Each capture rewrites one note per session, Calendar/Sessions/<date> <project> <id>.md
(what was asked, what was answered, files touched), and adds one line to today's
handoff note. No model is called; nothing leaves the laptop. Never fails a session:
any error is swallowed and logged to <vault>/Dev/learner/../capture.log.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from .context import VAULT
from . import handoff

CAPTURE_EVERY_TOKENS = int(os.environ.get("ATRIUM_CAPTURE_EVERY_TOKENS", 40000))
BYTES_PER_TOKEN = 4
SESS_DIR = VAULT / "Calendar" / "Sessions"
STATE = VAULT / "Dev" / "capture-state.json"
MAX_TURNS = 40


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def digest(transcript: Path) -> dict:
    asks, answers, files = [], [], []
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("message") or {}
        role = msg.get("role") or e.get("type")
        content = msg.get("content")
        if role == "user" and not e.get("isMeta"):
            t = _text(content).strip()
            if t and not t.startswith("<") and "tool_result" not in str(content)[:40]:
                asks.append(t)
        elif role == "assistant":
            t = _text(content).strip()
            if t:
                answers.append(t)
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in ("Write", "Edit", "MultiEdit"):
                    p = (c.get("input") or {}).get("file_path")
                    if p and p not in files:
                        files.append(p)
    return {"asks": asks, "answers": answers, "files": files}


def _short(t: str, n: int) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def render(meta: dict, d: dict) -> str:
    day = meta["started"][:10]
    lines = ["---", f"name: Session-{meta['session'][:8]}",
             f"description: Claude Code session in {meta['project']} on {day}, saved automatically.",
             f"updated: {dt.date.today().isoformat()}", "type: session", "status: done", "tags: [session, generated]",
             "protection: append", "---", "", f"# {meta['project']} session, {day}", "",
             f"_Saved automatically by `atrium.capture` ({meta['reason']}, ~{meta['tokens']:,} tokens). "
             "Facts worth keeping go to their home note; this is the record._", "", "## Asked"]
    lines += [f"- {_short(a, 300)}" for a in d["asks"][-MAX_TURNS:]] or ["- (nothing yet)"]
    lines += ["", "## Last answers"]
    lines += [f"- {_short(a, 400)}" for a in d["answers"][-8:]] or ["- (nothing yet)"]
    if d["files"]:
        lines += ["", "## Files touched"] + [f"- `{f}`" for f in d["files"][-30:]]
    return "\n".join(lines) + "\n"


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def handle(event: dict) -> str:
    if not VAULT.is_dir():
        return "no vault"
    tp = Path(event.get("transcript_path") or "")
    if not tp.is_file():
        return "no transcript"
    sid = event.get("session_id") or tp.stem
    name = event.get("hook_event_name", "Stop")
    size = tp.stat().st_size
    state = _load_state()
    s = state.get(sid, {"bytes": 0, "started": dt.datetime.now().isoformat(timespec="minutes"), "note": None})
    grown = size - s["bytes"]
    if name == "Stop" and grown < CAPTURE_EVERY_TOKENS * BYTES_PER_TOKEN:
        return "below threshold"
    project = Path(event.get("cwd") or ".").name or "home"
    meta = {"session": sid, "project": project, "started": s["started"], "tokens": size // BYTES_PER_TOKEN,
            "reason": {"Stop": f"every {CAPTURE_EVERY_TOKENS:,} tokens", "PreCompact": "before compaction",
                       "SessionEnd": "session end"}.get(name, name)}
    d = digest(tp)
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    note = s["note"] or str(SESS_DIR / f"{s['started'][:10]} {re.sub(r'[^A-Za-z0-9 _-]', '', project)} {sid[:8]}.md")
    Path(note).write_text(render(meta, d), encoding="utf-8")
    last_ask = _short(d["asks"][-1], 120) if d["asks"] else "—"
    handoff.remember("claude", [f"session {sid[:8]} in {project} saved ({meta['reason']}): last ask \"{last_ask}\" → [[{Path(note).stem}]]"])
    s.update(bytes=size, note=note)
    state[sid] = s
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    return f"saved {note}"


def main() -> int:
    try:
        event = json.load(sys.stdin)
        handle(event)
    except Exception as e:  # a hook must never break the session
        try:
            with (VAULT / "Dev" / "capture.log").open("a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {e!r}\n")
        except OSError:
            pass
    return 0
