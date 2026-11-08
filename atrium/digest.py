"""python -m atrium digest [--since HOURS] [--dry]: the laptop side, saved to the vault.

What happened on this laptop since the last digest, from local sources only:
Orchestra's runner.log (tasks started, passed, parked, merged, pushed; spend), the
Denisse repo's git log, the vault's git log (Don's notes, ingest results), Don's
progress pages that changed, and the queue (what's waiting, what needs Chuen).

The digest goes under its own heading in the day's chat note and adds one line to
the day's handoff note (atrium.chatlog, the same path the hub chat's digest uses);
only those two files are committed in the vault. The last digested moment is kept in
a small state file in the vault, so runs never repeat. When no activity section has a
line, nothing is written. Sources, wording, order, limits and the state file live in
atrium/config.json ("digest"); nothing from the vault is written to the repo.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

from . import chatlog, conf, handoff

REPO = Path(__file__).resolve().parent.parent


def _c() -> dict:
    return conf.section("digest")


def repo_root() -> Path:
    """settings.repo() (DENISSE_REPO / config.json), else the checkout this file is in."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        import settings
        return Path(settings.repo())
    except Exception:  # noqa: BLE001 -- the checkout itself is always a fine answer
        return REPO


# Every source keeps what falls in [since, now): now is taken (to the second) before any
# source is read, so a line stamped in now's own second may not exist yet; it goes to the
# next run, whose since is this now. Nothing is lost and nothing is told twice.
def _local(s: str) -> dt.datetime | None:
    """An ISO time as naive local time (git's %cI carries an offset; runner.log doesn't)."""
    try:
        t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


# -- state -----------------------------------------------------------------
def state_path(vault: Path) -> Path:
    return vault / _c()["state_file"]


def last_point(vault: Path) -> dt.datetime | None:
    try:
        return _local(json.loads(state_path(vault).read_text(encoding="utf-8"))["last"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _save_point(vault: Path, now: dt.datetime) -> None:
    p = state_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"last": now.isoformat(timespec="seconds")}), encoding="utf-8")
    tmp.replace(p)


# -- sources ---------------------------------------------------------------
def _runner(s: dict, since, now, repo: Path, vault: Path) -> list[str]:
    try:
        text = (repo / s["log"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    order: list[str] = []
    for v in s["events"].values():
        for label in (v.values() if isinstance(v, dict) else [v]):
            if label not in order:
                order.append(label)
    found: dict[str, list[str]] = {}
    spend, runs = 0.0, 0
    spend_re = re.compile(s["spend_pattern"])
    for line in text.splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 3:
            continue
        t = _local(parts[0])
        if not t or not (since <= t < now) or parts[1] not in s["events"]:
            continue
        ev, task, rest = s["events"][parts[1]], parts[2], parts[3] if len(parts) > 3 else ""
        for junk in s["task_strip"]:
            task = task.replace(junk, "")
        if isinstance(ev, dict):
            ev = next((label for start, label in ev.items() if rest.startswith(start)), None)
            if not ev:
                continue
        items = found.setdefault(ev, [])
        if task not in items:
            items.append(task)
        if parts[1] in s["spend_events"]:
            m = spend_re.search(rest)
            if m:
                spend += float(m.group(1))
                runs += 1
    lines = [s["line"].format(label=l, items=", ".join(found[l])) for l in order if l in found]
    if runs:
        lines.append(s["spend"].format(total=spend, runs=runs))
    return lines


def _git(s: dict, since, now, repo: Path, vault: Path) -> list[str]:
    where = repo if s["where"] == "repo" else vault
    c = conf.section("chatlog")
    try:
        out = subprocess.run([*c["git"], *s["args"], f"--since={since.isoformat(timespec='seconds')}"],
                             cwd=where, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=c["git_timeout_s"])
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode:
        return []
    skip = tuple(s["skip_prefixes"])
    lines = []
    for row in out.stdout.splitlines():
        bits = row.split("\t", 2)
        if len(bits) < 3:
            continue
        t = _local(bits[0])
        if not t or not (since <= t < now) or (skip and bits[2].startswith(skip)):
            continue
        lines.append(s["line"].format(hash=bits[1], subject=bits[2]))
    return lines[::-1]  # oldest first, like the other sections


def _pages(s: dict, since, now, repo: Path, vault: Path) -> list[str]:
    lo, hi = since.timestamp(), now.timestamp()
    seen, lines = set(), []
    for g in s["globs"]:
        for p in sorted(vault.glob(g)):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if p.is_file() and lo <= m < hi and p not in seen:
                seen.add(p)
                lines.append(s["line"].format(stem=p.stem, path=p.relative_to(vault).as_posix()))
    return lines


def _field(text: str, name: str) -> str:
    """A task file's "Name: value" header line (Orchestra's task format)."""
    for l in text.splitlines():
        if l.startswith(name + ":"):
            return l.split(":", 1)[1].strip()
    return ""


def _title(p: Path) -> tuple[str, str]:
    text = p.read_text(encoding="utf-8", errors="replace")
    return next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), p.stem), text


def _queue(s: dict, since, now, repo: Path, vault: Path) -> list[str]:
    review = repo / s["review"]
    n = s["title_chars"]
    waiting, drafts = [], []
    for p in sorted((review / "queue").glob("*.md")):
        try:
            title, text = _title(p)
        except OSError:
            continue
        (waiting if _field(text, "Queue").lower() == s["auto"] else drafts).append(title[:n])
    parked = []
    for p in sorted((review / "parked").glob("*.md")):
        if p.name.count(".") == 1:  # <slug>.md; its .report.md / .review.md siblings aren't tasks
            try:
                parked.append(_title(p)[0][:n])
            except OSError:
                continue
    lines = []
    if waiting:
        shown = waiting[:s["titles_max"]]
        lines.append(s["waiting"].format(n=len(waiting), titles=s["titles_lead"] + s["titles_sep"].join(shown)))
    lines += [s["draft"].format(task=t) for t in drafts]
    lines += [s["parked"].format(task=t) for t in parked]
    return lines


KINDS = {"runner": _runner, "git": _git, "pages": _pages, "queue": _queue}


# -- the digest ------------------------------------------------------------
def build(since: dt.datetime, now: dt.datetime, repo: Path, vault: Path) -> tuple[str, list[str], int]:
    """(markdown body, titles of sections with lines, line count); body "" = nothing happened."""
    c = _c()
    room, cut = c["max_lines"], c["line_chars"]
    blocks, titles, active, count = [], [], False, 0
    for s in c["sections"]:
        lines = [l if len(l) <= cut else l[:cut - len(c["ellipsis"])] + c["ellipsis"] for l in KINDS[s["kind"]](s, since, now, repo, vault)]
        if not lines:
            continue
        active = active or bool(s.get("counts_as_activity"))
        keep = min(len(lines), c["section_max_lines"], room)
        if keep <= 0:
            break
        shown = lines[:keep]
        if keep < len(lines):
            shown.append(c["more"].format(n=len(lines) - keep))
        room -= keep
        count += keep
        titles.append(s["title"])
        blocks.append(c["section_heading"].format(title=s["title"]) + "\n\n" + "\n".join(shown))
    if not active:
        return "", [], 0
    return "\n\n".join(blocks), titles, count


def run(since_hours: float | None = None, dry: bool = False, repo: Path | None = None,
        vault: Path | None = None, now: dt.datetime | None = None, commit: bool = True) -> str:
    c = _c()
    repo = Path(repo) if repo else repo_root()
    vault = Path(vault) if vault else handoff.VAULT
    now = now or dt.datetime.now().replace(microsecond=0)
    if since_hours is not None:
        since = now - dt.timedelta(hours=since_hours)
    else:
        since = last_point(vault) or now - dt.timedelta(hours=c["first_run_hours"])
    label = since.strftime(c["since_format"])
    body, titles, count = build(since, now, repo, vault)
    if dry:
        return c["dry_header"] + "\n\n" + (body or c["nothing"].format(since=label))
    if not body:
        _save_point(vault, now)
        return c["nothing"].format(since=label)
    out = chatlog.save("atrium", body, vault=vault, now=now, commit=commit, since=label, lines=count,
                       sections=", ".join(titles))
    _save_point(vault, now)
    msg = c["saved"].format(note=out["note"])
    return msg + (f" (not committed: {out['error']})" if out["error"] else "")
