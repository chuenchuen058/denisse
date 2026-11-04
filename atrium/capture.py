"""Save Claude Code sessions into the vault on their own (after Thomas's
compact-retention and handoff hooks).

Runs as a Claude Code hook (hook JSON on stdin: session_id, transcript_path, cwd,
hook_event_name). A session is captured
  * on Stop, once it has grown by CAPTURE_EVERY_TOKENS since the last capture,
    so long sessions are saved as they go, not only at the end;
  * always on every other event (shipped: PreCompact, before Claude Code drops
    detail, and SessionEnd).

Each capture rewrites one note per session under the sessions folder (what was
asked, what was answered, files touched), and adds one line to today's handoff
note. No model is called; nothing leaves the laptop. Never fails a session: any
error is swallowed and logged to the capture log. Thresholds, folders, caps and
every line of the note live in atrium/config.json ("capture").
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from . import conf
from .context import VAULT
from . import handoff

_C = conf.section("capture")
CAPTURE_EVERY_TOKENS = int(os.environ.get(_C["every_tokens_env"], _C["every_tokens"]))
BYTES_PER_TOKEN = _C["bytes_per_token"]
SESS_DIR = VAULT / _C["sessions_dir"]
STATE = VAULT / _C["state_file"]
MAX_TURNS = _C["max_asks"]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def digest(transcript: Path) -> dict:
    """Parse Claude Code's transcript JSONL (a fixed format)."""
    edit_tools = set(conf.section("capture")["edit_tools"])
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
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in edit_tools:
                    p = (c.get("input") or {}).get("file_path")
                    if p and p not in files:
                        files.append(p)
    return {"asks": asks, "answers": answers, "files": files}


def _short(t: str, n: int) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[: n - 1] + conf.section("capture")["ellipsis"]


def render(meta: dict, d: dict) -> str:
    c = conf.section("capture")
    day = meta["started"][:10]
    f = {"id": meta["session"][:c["id_chars"]], "project": meta["project"], "day": day,
         "today": dt.date.today().isoformat(), "reason": meta["reason"], "tokens": meta["tokens"]}
    lines = ["---", *(l.format(**f) for l in c["front_matter"]), "---", "", c["title"].format(**f), "",
             c["intro"].format(**f), "", c["asked_heading"]]
    lines += [f"- {_short(a, c['ask_chars'])}" for a in d["asks"][-MAX_TURNS:]] or [c["empty"]]
    lines += ["", c["answers_heading"]]
    lines += [f"- {_short(a, c['answer_chars'])}" for a in d["answers"][-c["max_answers"]:]] or [c["empty"]]
    if d["files"]:
        lines += ["", c["files_heading"]] + [f"- `{x}`" for x in d["files"][-c["max_files"]:]]
    return "\n".join(lines) + "\n"


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def handle(event: dict) -> str:
    c = conf.section("capture")
    if not VAULT.is_dir():
        return "no vault"
    tp = Path(event.get("transcript_path") or "")
    if not tp.is_file():
        return "no transcript"
    sid = event.get("session_id") or tp.stem
    name = event.get("hook_event_name", c["default_event"])
    size = tp.stat().st_size
    state = _load_state()
    s = state.get(sid, {"bytes": 0, "started": dt.datetime.now().isoformat(timespec="minutes"), "note": None})
    grown = size - s["bytes"]
    if name == c["threshold_event"] and grown < CAPTURE_EVERY_TOKENS * BYTES_PER_TOKEN:
        return "below threshold"
    project = Path(event.get("cwd") or ".").name or c["default_project"]
    reason = c["reasons"].get(name, name).format(every=CAPTURE_EVERY_TOKENS)
    meta = {"session": sid, "project": project, "started": s["started"], "tokens": size // BYTES_PER_TOKEN,
            "reason": reason}
    d = digest(tp)
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    short_id = sid[:c["id_chars"]]
    note = s["note"] or str(SESS_DIR / c["note_name"].format(
        day=s["started"][:10], project=re.sub(r"[^A-Za-z0-9 _-]", "", project), id=short_id))
    Path(note).write_text(render(meta, d), encoding="utf-8")
    last_ask = _short(d["asks"][-1], c["handoff_ask_chars"]) if d["asks"] else c["handoff_no_ask"]
    handoff.remember(c["handoff_agent"], [c["handoff_line"].format(
        id=short_id, project=project, reason=reason, ask=last_ask, note=Path(note).stem)])
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
            with (VAULT / conf.section("capture")["log_file"]).open("a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {e!r}\n")
        except Exception:
            pass
    return 0
