"""Import bounded chat summaries, locally and dry-run by default.

python -m atrium.import_chats --source codex --vault <vault> [--path <sessions>] [--apply]
Only Calendar/Chats/*.md and Dev/chat-import-state.json are written. State keys
are source-qualified conversation IDs; a missing ID falls back to the file path.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from orchestra.aftercare import SECRET_TEXT
from .capture import _short as short

SOURCES = ("claude-code", "codex", "claude-export")
STATE = Path("Dev/chat-import-state.json")
CHATS = Path("Calendar/Chats")
PASSWORD = re.compile(r"password\s*:|passwd", re.I)
# Cover labelled credentials as well as the provider-specific aftercare patterns.
CREDENTIAL = re.compile(r"\b(?:api[ _-]?key|access[ _-]?token|auth[ _-]?token|"
                        r"refresh[ _-]?token|token|secret|password)\b\s*[:=]", re.I)


def clean(text: str) -> str:
    """Remove whole credential lines BEFORE deriving any summaries or titles."""
    kept, private_key = [], False
    for line in text.splitlines():
        if re.search(r"-----BEGIN .*PRIVATE KEY-----", line):
            private_key = True
        if private_key:
            if re.search(r"-----END .*PRIVATE KEY-----", line):
                private_key = False
            continue
        if not (SECRET_TEXT.search(line) or PASSWORD.search(line) or CREDENTIAL.search(line)):
            kept.append(line)
    return "\n".join(kept).strip()


def markdown(text: str) -> str:
    # Quoted chat prose must not become vault links, embeds, HTML or formatting.
    return re.sub(r"([\\`*\[\]()<>_])", r"\\\1", text)


def title(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', " ", clean(text))
    return short(text, 60).rstrip(" .") or "Untitled chat"


@dataclass
class Chat:
    id: str
    date: str
    title: str
    asks: list[str]
    answers: list[str]
    origin: str


def events(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                continue  # interrupted JSONL writes and malformed lines
            if isinstance(event, dict):
                out.append(event)
    return out


def day(value, path: Path) -> str:
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c["text"] for c in content if isinstance(c, dict)
                         and c.get("type") in ("text", "input_text", "output_text")
                         and isinstance(c.get("text"), str))
    return ""


def jsonl_chat(source: str, path: Path) -> Chat:
    entries = events(path)
    started = next((e["timestamp"] for e in entries if e.get("timestamp")), None)
    sid = None
    if source == "claude-code":
        sid = next((e["sessionId"] for e in entries if e.get("sessionId")), None)
        asks, answers = [], []
        # capture.digest flattens blocks with spaces and suppresses all '<...'
        # user text. Imports need original line boundaries for redaction and
        # must retain genuine questions about markup, so parse those here.
        for e in entries:
            msg = e.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or e.get("type")
            text = content_text(msg.get("content"))
            if text.strip():
                if role == "user" and not e.get("isMeta"):
                    asks.append(text)
                elif role == "assistant":
                    answers.append(text)
    else:
        messages = {"user": [], "assistant": []}
        legacy = {"user": [], "assistant": []}
        for e in entries:
            p = e.get("payload")
            if not isinstance(p, dict):
                continue
            if e.get("type") == "session_meta":
                sid = p.get("id") or sid
                started = p.get("timestamp") or started
            elif e.get("type") == "response_item" and p.get("type") == "message":
                role = p.get("role")
                if role in messages and p.get("channel") != "analysis":
                    text = content_text(p.get("content"))
                    if text.strip():
                        messages[role].append(text)
            elif e.get("type") == "event_msg":
                role = {"user_message": "user", "agent_message": "assistant"}.get(p.get("type"))
                if role and isinstance(p.get("message"), str) and p["message"].strip():
                    legacy[role].append(p["message"])
        # Codex commonly mirrors the same turns in both streams. Prefer the
        # response stream per role, preserving deliberately repeated questions.
        asks = messages["user"] or legacy["user"]
        answers = messages["assistant"] or legacy["assistant"]
    identity = str(sid or path.resolve())
    return Chat(identity, day(started, path), "", asks, answers, str(path.resolve()))


def conversations(source: str, path: Path):
    if source not in SOURCES:
        raise ValueError("unknown chat source")
    if source != "claude-export":
        if path.is_file():
            if path.suffix != ".jsonl":
                raise ValueError("session input must be JSONL")
            paths = [path]
        elif path.is_dir():
            paths = sorted(path.rglob("*.jsonl"))
        else:
            raise ValueError("session input does not exist")
        for file in paths:
            yield jsonl_chat(source, file)
        return
    file = path / "conversations.json" if path.is_dir() else path
    data = json.loads(file.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError("conversations.json must contain a list")
    for i, c in enumerate(data):
        if not isinstance(c, dict) or not isinstance(c.get("chat_messages"), list):
            raise ValueError("invalid exported conversation")
        asks, answers = [], []
        for m in c["chat_messages"]:
            if not isinstance(m, dict):
                continue
            text = m.get("text")
            if isinstance(text, str) and text.strip():
                if m.get("sender") == "human":
                    asks.append(text)
                elif m.get("sender") == "assistant":
                    answers.append(text)
        sid = str(c.get("uuid") or c.get("id") or f"{file.resolve()}#{i}")
        yield Chat(sid, day(c.get("created_at"), file),
                   c.get("name") if isinstance(c.get("name"), str) else "",
                   asks, answers, sid)


def render(chat: Chat, source: str, stem: str) -> str:
    # JSON string literals are valid YAML quoted scalars, including colons,
    # quotes and newlines; no chat text can inject a frontmatter property.
    def quoted(text):
        # Keep quoted references inert for the vault checker's link scan too.
        return json.dumps(text, ensure_ascii=False).replace("[", r"\u005b").replace("]", r"\u005d")
    lines = ["---", f"name: {quoted(stem)}",
             f"description: {quoted(short(chat.asks[0], 300))}",
             f"updated: {dt.date.today().isoformat()}", "type: chat",
             f"source: {source}", f"date: {chat.date}",
             f"tags: [chat, {source}]", "---", "", f"# {markdown(stem)}", "",
             "## What was asked", ""]
    lines += [f"- {markdown(short(a, 300))}" for a in chat.asks]
    outcome = short(chat.answers[-1], 800) if chat.answers else ""
    lines += ["", "## Outcome", "", markdown(outcome) or "(No assistant text available.)",
              "", "## Original", "", markdown(short(clean(chat.origin), 1000)) or "(Redacted.)", ""]
    return "\n".join(lines)


def checked(vault: Path, relative: Path) -> Path:
    path = vault / relative
    # Reject redirected parents as well as symlinked files, including junctions
    # on Windows. State contents are never used as a write destination.
    if not path.is_relative_to(vault) or path.resolve() != path:
        raise ValueError("import output path is redirected outside its designated location")
    return path


def import_chats(source: str, path: Path, vault: Path, *, apply: bool = False) -> list[Path]:
    vault = Path(vault).expanduser().resolve()
    path = Path(path).expanduser()
    state_path = checked(vault, STATE)
    checked(vault, CHATS)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not isinstance(state, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                             for k, v in state.items()):
        raise ValueError("invalid chat import state; refusing to overwrite it")
    written, reserved = [], set()
    for chat in conversations(source, path):
        identity = chat.id
        if clean(identity) != identity:
            identity = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        key = f"{source}:{identity}"
        if key in state:
            continue
        chat.asks = [t for a in chat.asks if (t := clean(a))]
        chat.answers = [clean(a) for a in chat.answers]
        if not chat.asks:
            continue
        label = title(chat.title or chat.asks[0])
        stem = f"{chat.date} {source} {label}"
        note = checked(vault, CHATS / (stem + ".md"))
        n = 1
        while note.exists() or note.name.casefold() in reserved:
            suffix = f" ({n})"
            note = checked(vault, CHATS / f"{chat.date} {source} {label[:60-len(suffix)].rstrip(' .')}{suffix}.md")
            n += 1
        reserved.add(note.name.casefold())
        if apply:
            checked(vault, CHATS).mkdir(parents=True, exist_ok=True)
            checked(vault, STATE.parent).mkdir(parents=True, exist_ok=True)
            checked(vault, note.relative_to(vault))
            with note.open("x", encoding="utf-8", newline="\n") as f:
                f.write(render(chat, source, note.stem))
        state[key] = note.relative_to(vault).as_posix()
        if apply:
            checked(vault, STATE).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
        print(f"{'Wrote' if apply else 'Would write'} {note}")
        written.append(note)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=SOURCES)
    parser.add_argument("--path", type=Path,
                        help="source file/directory (defaults to the selected CLI's session directory)")
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="write notes and import state")
    args = parser.parse_args(argv)
    if args.path is None:
        defaults = {"claude-code": Path.home() / ".claude/projects",
                    "codex": Path.home() / ".codex/sessions"}
        if args.source not in defaults:
            parser.error("--path is required for claude-export")
        args.path = defaults[args.source]
    try:
        notes = import_chats(args.source, args.path, args.vault, apply=args.apply)
    except (OSError, ValueError, TypeError, AttributeError):
        # Exception details may contain private text from malformed input/state.
        print("Import failed: check input/state format and output permissions; existing imports were retained.",
              file=sys.stderr)
        return 1
    print(f"{len(notes)} chat(s) {'imported' if args.apply else 'planned (dry run; nothing written)'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
