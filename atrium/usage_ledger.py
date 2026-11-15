"""Tokens spent, per day and per agent, from every source on this laptop, kept in a running ledger.

    python -m atrium.usage_ledger          # bring the ledger up to date and print its totals

Sources (atrium/config.json "live" -> "board" -> "tokens" -> "sources", each with its notes):
- "orchestra": the runner's own record of each finished run (state.json history -> tokens).
- "claude_code": Claude Code's session logs (one JSON object per line; assistant lines carry a
  message id and a usage object). Each message counts once: the same id is written once per
  content block, and resumed sessions copy old messages into a new file, so ids seen are kept.
- "codex": Codex's session logs; each token_count event carries the session's running total, so
  a file adds the difference from the last total read in it.

Orchestra's own Claude runs are in state.json AND in Claude Code's logs. With prefer_logs on the
claude_code source (15 Nov 2026: state.json recorded 0.61B where the worktree logs held ~0.9B) the
logs win: every project is read, worktrees included, the session ids found are kept in the ledger,
and state.json counts only the runs whose session no log holds (no session id, or its log is
gone). Without it the old way: the worktree projects (skip_dirs) and any line whose session id a
finished Orchestra run recorded are skipped, and state.json counts every run.

The ledger (.review/<ledger>, git-ignored) holds day x agent totals and how far each log file has
been read (size and byte offset), so each refresh reads only what was appended, and totals
survive Claude Code pruning its old logs. Orchestra's days are rewritten from state.json on each
refresh; a day state.json no longer has is kept from the ledger. The live page never writes to
.review, so it reads the ledger and carries on from it in memory; the ledger is written by
`python -m atrium.usage_ledger` and `python -m atrium snapshot` (the hub refresh).
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
import time
from pathlib import Path

from . import conf, snapshot


def _cfg() -> dict:
    return conf.section("live")["board"]["tokens"]


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p)))


def _get(obj, dotted: str):
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _usage(usage, keys: dict, minus: dict | None = None) -> dict:
    """{in, out, cache_read, cache_write} as ints from a log's usage object (missing = 0)."""
    if not isinstance(usage, dict):
        return {}
    out = {k: usage.get(f) if isinstance(usage.get(f), int) else 0 for k, f in keys.items()}
    for k, less in (minus or {}).items():
        out[k] = max(0, out.get(k, 0) - sum(out.get(m, 0) for m in less))
    return out


def _add(days: dict, day: str, agent: str, t: dict) -> None:
    row = days.setdefault(day, {}).setdefault(agent, {})
    for k, n in t.items():
        row[k] = row.get(k, 0) + int(n or 0)


def _day(stamp) -> str | None:
    when = snapshot._local_dt(stamp) if stamp else None
    return when.date().isoformat() if when else None


def ledger_path(c: dict | None = None) -> Path:
    return snapshot.REVIEW / (c or _cfg())["ledger"]


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    for k in ("logs", "orchestra", "files", "seen", "sessions"):
        data[k] = data.get(k) if isinstance(data.get(k), dict) else {}
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _orchestra(state: dict, src: dict, all_keys: list, logged=None) -> tuple[dict, set]:
    """Day x agent totals of the runs state.json records, and the session ids of those runs.
    A run whose session id is in logged is left out (its log already counted it); every day
    with a run gets an entry, empty when all its runs were logged, so an older total is replaced."""
    days, sessions = {}, set()
    for ts in (state.get("tasks") or {}).values():
        for h in (ts or {}).get("history", []) or []:
            if not isinstance(h, dict):
                continue
            sid = h.get(src["session_field"]) if isinstance(h.get(src["session_field"]), str) else None
            if sid:
                sessions.add(sid)
            day, t = _day(h.get(src["time_field"])), h.get(src["tokens_field"])
            if day:
                days.setdefault(day, {})
            if sid and logged and sid in logged:
                continue
            if day and isinstance(t, dict):
                _add(days, day, h.get(src["agent_field"]) or src["agent_unknown"],
                     {k: int(t.get(k) or 0) for k in all_keys})
    return days, sessions


def _files(src: dict):
    root = _expand(src["root"])
    if not root.is_dir():
        return
    skip = [] if src.get("prefer_logs") else src.get("skip_dirs") or []
    for p in sorted(root.glob(src["glob"])):
        rel = p.relative_to(root).parts[:-1]
        if any(fnmatch.fnmatch(d, pat) for d in rel for pat in skip):
            continue
        if p.is_file():
            yield p


def _new_lines(p: Path, mark: dict):
    """The complete lines appended to p since the offset in mark (updated in place)."""
    try:
        size = p.stat().st_size
    except OSError:
        return
    offset = int(mark.get("offset") or 0)
    if size < offset:  # rewritten or truncated: read it again (ids and last totals still guard)
        offset = 0
    if size == offset:
        mark["size"] = size
        return
    try:
        with open(p, "rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
    except OSError:
        return
    end = chunk.rfind(b"\n")
    if end < 0:
        return
    mark["offset"], mark["size"] = offset + end + 1, size
    yield from chunk[:end].split(b"\n")


def _read_claude_code(src: dict, data: dict, all_from: str, orch_sessions: set) -> None:
    marker = src["marker"].encode()
    f = src["fields"]
    for p in _files(src):
        mark = data["files"].setdefault(str(p), {})
        for raw in _new_lines(p, mark):
            if marker not in raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            mid, usage = _get(obj, f["id"]), _get(obj, f["usage"])
            if not isinstance(mid, str) or not isinstance(usage, dict):
                continue
            sid = _get(obj, f["session"])
            if (not src.get("prefer_logs") and sid in orch_sessions) or mid in data["seen"]:
                continue
            day = _day(_get(obj, f["time"]))
            if not day or day < all_from:
                continue
            data["seen"][mid] = day
            if src.get("prefer_logs") and isinstance(sid, str):
                data["sessions"].setdefault(sid, day)
            _add(data["logs"], day, src["agent"], _usage(usage, src["keys"], src.get("minus")))


def _read_codex(src: dict, data: dict, all_from: str, orch_sessions: set) -> None:
    marker = src["marker"].encode()
    f = src["fields"]
    for p in _files(src):
        mark = data["files"].setdefault(str(p), {})
        for raw in _new_lines(p, mark):
            if marker not in raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if _get(obj, f["type"]) != src["type_is"]:
                continue
            total = _usage(_get(obj, f["usage"]), src["keys"], src.get("minus"))
            if not total:
                continue
            last = mark.get("last") or {}
            delta = {k: max(0, n - int(last.get(k) or 0)) for k, n in total.items()}
            mark["last"] = total
            day = _day(_get(obj, f["time"]))
            if not day or day < all_from or not any(delta.values()):
                continue
            _add(data["logs"], day, src["agent"], delta)


READERS = {"claude_code": _read_claude_code, "codex": _read_codex}


# ledger path -> (when its logs were last read, the ledger file's mtime then, the ledger in memory)
_mem: dict[str, tuple[float, float | None, dict]] = {}


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def update(state: dict, now: dt.datetime, save: bool = False) -> dict:
    """Bring the ledger up to date and return {"days": {day: {agent: {key: n}}}} (logs plus
    Orchestra). Orchestra's runs are read every call; the logs at most every scan_s, from where
    the last read stopped. save=False (the live page, which never writes to .review) keeps the
    progress in memory only; save=True writes the ledger. Never raises on a bad log: a line that
    does not parse is skipped."""
    c = _cfg()
    all_from = c.get("all_from") or ""
    path = ledger_path(c)
    last, seen_mtime, data = _mem.get(str(path), (None, None, None))
    scan = last is None or time.monotonic() - last >= c["scan_s"]
    disk_mtime = _mtime(path)
    if data is None or (scan and disk_mtime != seen_mtime):  # another process saved it since
        data = _load(path)
        seen_mtime = disk_mtime
    orch = next((s for s in c["sources"] if s["kind"] == "orchestra"), None)
    sessions = _orchestra(state or {}, orch, c["all_keys"])[1] if orch else set()
    if scan:
        for src in c["sources"]:
            if src["kind"] in READERS:
                READERS[src["kind"]](src, data, all_from, sessions)
        keep_from = (now.date() - dt.timedelta(days=c["seen_keep_days"])).isoformat()
        data["seen"] = {k: d for k, d in data["seen"].items() if d >= keep_from}
        last = time.monotonic()
    # session ids a log held are kept for good (not pruned like "seen"): a run whose log is
    # gone must not come back from state.json on top of the day total its log already gave
    logged = data["sessions"] if any(s.get("prefer_logs") for s in c["sources"]) else None
    orch_days = _orchestra(state or {}, orch, c["all_keys"], logged)[0] if orch else {}
    changed = any(data["orchestra"].get(d) != v for d, v in orch_days.items())
    data["orchestra"].update(orch_days)
    if save and (scan or changed):
        try:
            _save(path, data)
            seen_mtime = _mtime(path)
        except OSError:
            pass
    _mem[str(path)] = (last, seen_mtime, data)
    days: dict = {}
    for part in (data["logs"], data["orchestra"]):
        for day, agents in part.items():
            for agent, t in agents.items():
                _add(days, day, agent, t)
    return {"days": days}


def save() -> dict:
    """Read what is new in the logs and write the ledger; the day table. (python -m atrium.usage_ledger,
    and python -m atrium snapshot, which already writes the hub tables into .review.)"""
    return update(snapshot._state(), dt.datetime.now(), save=True)["days"]


def main() -> int:
    days = save()
    keys = _cfg()["all_keys"]
    for day in sorted(days):
        print(day, "  ".join(f"{a} {sum(t.get(k, 0) for k in keys):,}" for a, t in sorted(days[day].items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
