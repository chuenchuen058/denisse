"""Import bounded chat summaries, locally and dry-run by default.

python -m atrium.import_chats --source codex --vault <vault> [--path <sessions>] [--apply]
python -m atrium import-chats --link-existing [--dry-run] [--vault <vault>]
Imports write only the chats folder's notes and the import state, plus lines
added to an existing day note's Chats section. Every chat note gets a Related
line to the day, handoff and project/module notes that exist (atrium/config.json
"import_chats"). State keys are source-qualified conversation IDs; a missing ID
falls back to the file path.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from orchestra.aftercare import SECRET_TEXT
from . import chatlog, conf
from .capture import _short as short

SOURCES = ("claude-code", "codex", "claude-export")
STATE = Path(conf.section("import_chats")["state_file"])
CHATS = Path(conf.section("import_chats")["chats_folder"])
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


def conversations(source: str, path: Path, settled_before: float | None = None):
    """Yield chats; JSONL files modified at/after settled_before are skipped
    (a session still being written), so a later run picks them up whole."""
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
            if settled_before is not None and file.stat().st_mtime >= settled_before:
                continue
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
             f"tags: [chat, {source}]",
             f"protection: {conf.section('import_chats')['chat_protection']}",
             "---", "", f"# {markdown(stem)}", "",
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


# ---- links: chat note -> day/handoff/project notes, day note -> its chats ----

def _lc() -> dict:
    return conf.section("import_chats")


def frontmatter(text: str) -> tuple[dict, list[str]]:
    """({key: raw value or list}, body lines). Only the shapes vault notes use:
    `key: value`, `key: [a, b]` and a `key:` followed by `- item` lines."""
    lines = text.lstrip("﻿").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, lines
    fm, key = {}, None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return fm, lines[i + 1:]
        if key and re.match(r"\s+-\s", line):
            fm[key].append(line.split("-", 1)[1].strip().strip("\"'"))
            continue
        k, sep, v = line.partition(":")
        if sep and k and not k[0].isspace():
            key, v = k.strip(), v.strip()
            if not v:
                fm[key] = []
            elif v.startswith("[") and v.endswith("]"):
                fm[key] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
            else:
                fm[key] = v
                key = None
    return {}, lines


def _scalar(fm: dict, key: str) -> str:
    v = fm.get(key)
    if isinstance(v, str):
        try:
            v = json.loads(v) if v.startswith('"') else v
        except ValueError:
            pass
        return str(v).strip().strip("'")
    return ""


def _rel(vault: Path, note: Path) -> str:
    return note.relative_to(vault).with_suffix("").as_posix()


def _link(vault: Path, note: Path) -> str | None:
    c = _lc()
    rel = _rel(vault, note)
    if any(ch in rel for ch in c["link_unsafe_chars"]):
        return None  # Obsidian would read it as a heading/block/alias, not this note
    return c["link"].format(path=rel, name=note.stem)


def _phrase(term: str) -> re.Pattern:
    words = [re.escape(w) for w in term.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.I)


def note_index(vault: Path) -> list[tuple[Path, list[re.Pattern]]]:
    """Candidate project/module notes with the phrase patterns that mean them."""
    m = _lc()["match"]
    notes = {}
    for pattern in m["candidate_globs"]:
        for p in vault.glob(pattern):
            if p.is_file() and not any(fnmatch.fnmatch(p.stem, x) for x in m["exclude_names"]):
                notes[p] = None
    owners: dict[str, set[Path]] = {}
    for p in notes:
        try:
            fm, _ = frontmatter(p.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            continue
        aliases = fm.get(m["aliases_field"], [])
        terms = {p.stem, *([aliases] if isinstance(aliases, str) else aliases),
                 *m["extra_aliases"].get(p.stem, [])}
        terms |= {p.stem[:-len(s)] for s in m["drop_suffixes"]
                  if p.stem.casefold().endswith(s.casefold())}
        for t in terms:
            t = " ".join(str(t).split()).casefold()
            if len(t) >= m["min_term_chars"]:
                owners.setdefault(t, set()).add(p)
    by_note: dict[Path, list[re.Pattern]] = {}
    for t, ps in owners.items():
        if len(ps) == 1:  # a term two notes share points at neither
            by_note.setdefault(next(iter(ps)), []).append(_phrase(t))
    return sorted(by_note.items())


def chat_units(text: str) -> list[str]:
    """The pieces of a chat note the matcher reads: its title and the bullets of
    the match_sections, with the note's markdown escaping undone."""
    m = _lc()["match"]
    _, body = frontmatter(text)
    units, inside = [], False
    for line in body:
        if line.startswith("# ") and m["match_title"]:
            units.append(line[2:])
        if line.startswith("#"):
            inside = line.strip() in m["match_sections"]
        elif inside and line.startswith("- "):
            units.append(line[2:])
    return [re.sub(r"\\(.)", r"\1", u) for u in units]


def project_notes(units: list[str], index) -> list[Path]:
    m = _lc()["match"]
    scored = []
    for p, patterns in index:
        score = sum(1 for u in units if any(r.search(u) for r in patterns))
        if score >= m["min_score"]:
            scored.append((-score, len(p.parts) if m["prefer_shallow"] else 0, p.stem.casefold(), p))
    return [s[-1] for s in sorted(scored)]


def day_notes(vault: Path, date: str) -> list[Path]:
    if not date:
        return []
    paths = [vault / _lc()["day_note"].format(date=date),
             vault / conf.section("handoff")["note"].format(date=date)]
    return [p for p in paths if p.is_file()]


def related_line(vault: Path, note: Path, date: str, text: str, index) -> str:
    """'' when nothing exists to link to (never a link to a missing note)."""
    c = _lc()
    links = []
    for target in [*day_notes(vault, date), *project_notes(chat_units(text), index)]:
        link = _link(vault, target) if target != note else None
        if link and link not in links:
            links.append(link)
    links = links[:c["max_related_links"]]
    return c["related_line"].format(links=c["related_join"].join(links)) if links else ""


def writable(fm: dict, kind: str) -> bool:
    p = _lc()["protection"]
    value = _scalar(fm, p["field"]) or p["missing"].get(kind, "locked")
    return value in p["writable"]


def _newline(raw: str) -> str:
    return "\r\n" if "\r\n" in raw else "\n"


def add_lines(vault: Path, note: Path, lines: list[str], after: int | None = None) -> None:
    """Add lines to a note without changing any existing byte: appended at the
    end (after a blank line), or inserted after body line index `after`."""
    chatlog._guard(vault, note)
    checked(vault, note.relative_to(vault))
    with note.open(encoding="utf-8", newline="") as f:
        raw = f.read()
    nl = _newline(raw)
    if after is None:
        trailing = raw[len(raw.rstrip("\r\n")):].count("\n")
        lead = nl * max(0, 2 - trailing) if raw.strip() else ""
        with note.open("a", encoding="utf-8", newline="") as f:
            f.write(lead + nl.join(lines) + nl)
        return
    parts = raw.split(nl)
    parts[after + 1:after + 1] = lines
    with note.open("w", encoding="utf-8", newline="") as f:
        f.write(nl.join(parts))


def _chats_section_end(lines: list[str], heading: str) -> int | None:
    """Index of the last non-blank line of the day_section, or None if absent."""
    level = len(heading) - len(heading.lstrip("#"))
    start = next((i for i, l in enumerate(lines) if l.rstrip("\r") == heading), None)
    if start is None:
        return None
    end = start
    for i in range(start + 1, len(lines)):
        l = lines[i].rstrip("\r")
        if l.startswith("#") and len(l) - len(l.lstrip("#")) <= level:
            break
        if l.strip():
            end = i
    return end


def link_day(vault: Path, date: str, chats: list[Path], apply: bool) -> tuple[int, bool]:
    """Add the day's missing chat links to its day note. -> (lines added, locked)."""
    c = _lc()
    day = vault / c["day_note"].format(date=date)
    if not day.is_file():
        return 0, False
    with day.open(encoding="utf-8", newline="") as f:
        raw = f.read()
    fm, _ = frontmatter(raw.replace("\r\n", "\n"))
    missing = []
    for p in sorted(set(chats)):
        link = _link(vault, p)
        # Already linked from the day, in any link form, counts as listed.
        if link and not any(f"[[{t}{end}" in raw for t in (_rel(vault, p), p.stem) for end in "]|"):
            missing.append(c["day_line"].format(link=link))
    if not missing:
        return 0, False
    if not writable(fm, "day"):
        return 0, True
    if apply:
        end = _chats_section_end(raw.split(_newline(raw)), c["day_section"])
        if end is None:
            add_lines(vault, day, [c["day_section"], "", *missing])
        else:
            add_lines(vault, day, missing, after=end)
    return len(missing), False


def import_chats(source: str, path: Path, vault: Path, *, apply: bool = False,
                 limit: int | None = None, settled_before: float | None = None) -> list[Path]:
    """limit caps the notes written this run; the rest stay unrecorded in the
    state, so the next run continues where this one stopped."""
    vault = Path(vault).expanduser().resolve()
    path = Path(path).expanduser()
    state_path = checked(vault, STATE)
    checked(vault, CHATS)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not isinstance(state, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                             for k, v in state.items()):
        raise ValueError("invalid chat import state; refusing to overwrite it")
    written, reserved, dates = [], set(), {}
    index = note_index(vault)
    for chat in conversations(source, path, settled_before):
        if limit is not None and len(written) >= limit:
            break
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
        text = render(chat, source, note.stem)
        if related := related_line(vault, note, chat.date, text, index):
            text += "\n" + related + "\n"
        if apply:
            checked(vault, CHATS).mkdir(parents=True, exist_ok=True)
            checked(vault, STATE.parent).mkdir(parents=True, exist_ok=True)
            checked(vault, note.relative_to(vault))
            with note.open("x", encoding="utf-8", newline="\n") as f:
                f.write(text)
        state[key] = note.relative_to(vault).as_posix()
        if apply:
            checked(vault, STATE).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
        print(f"{'Wrote' if apply else 'Would write'} {note}")
        written.append(note)
        dates.setdefault(chat.date, []).append(note)
    if apply:
        for date, notes in dates.items():
            link_day(vault, date, notes, apply=True)
    return written


def link_existing(vault: Path, *, apply: bool = False) -> dict:
    """Backfill: give existing chat notes their Related line and list them in
    their day notes. Only adds lines; a note that already has a Related line,
    and a chat the day note already links, are left alone (so it is idempotent)."""
    c = _lc()
    vault = Path(vault).expanduser().resolve()
    folder = checked(vault, CHATS)
    out = {"chats": 0, "already": 0, "empty": 0, "locked_chats": 0,
           "days": 0, "lines": 0, "locked_days": 0, "sample": []}
    if not folder.is_dir():
        return out
    index = note_index(vault)
    by_date: dict[str, list[Path]] = {}
    for note in sorted(folder.glob("*.md")):
        text = note.read_text(encoding="utf-8-sig", errors="replace")
        fm, body = frontmatter(text)
        if _scalar(fm, "type") != c["chat_type"]:
            continue
        date = _scalar(fm, "date")
        if date:
            by_date.setdefault(date, []).append(note)
        if any(l.startswith(c["related_prefix"]) for l in body):
            out["already"] += 1
        elif not writable(fm, "chat"):
            out["locked_chats"] += 1
        elif not (related := related_line(vault, note, date, text, index)):
            out["empty"] += 1
        else:
            out["chats"] += 1
            if len(out["sample"]) < c["dry_run_sample"]:
                out["sample"].append(c["sample_line"].format(
                    chat=note.stem, links=related[len(c["related_prefix"]):].strip()))
            if apply:
                add_lines(vault, note, [related])
    for date, notes in sorted(by_date.items()):
        added, locked = link_day(vault, date, notes, apply)
        out["lines"] += added
        out["days"] += bool(added)
        out["locked_days"] += locked
    return out


def report(out: dict, apply: bool) -> str:
    c = _lc()
    lines = [] if apply else [c["dry_header"]]
    lines.append(c["report"].format(verb=c["verb_apply" if apply else "verb_dry"],
                                    **{k: v for k, v in out.items() if k != "sample"}))
    return "\n".join(lines + out["sample"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=SOURCES)
    parser.add_argument("--path", type=Path,
                        help="source file/directory (defaults to the selected CLI's session directory)")
    parser.add_argument("--vault", type=Path,
                        help="the vault (required to import; --link-existing defaults to Chuen's vault)")
    parser.add_argument("--apply", action="store_true", help="write notes and import state")
    parser.add_argument("--link-existing", action="store_true",
                        help="backfill Related lines and day-note Chats sections on notes already imported")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --link-existing: print counts and a sample, write nothing")
    args = parser.parse_args(argv)
    if args.link_existing:
        from .context import VAULT
        try:
            out = link_existing(args.vault or VAULT, apply=not args.dry_run)
        except (OSError, ValueError):
            print(_lc()["failed"], file=sys.stderr)
            return 1
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(report(out, apply=not args.dry_run))
        return 0
    if args.source is None or args.vault is None:
        parser.error("--source and --vault are required to import")
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
