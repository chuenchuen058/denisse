"""After each Orchestra task: a note in the Obsidian vault, and (after a merge) a guarded push.

Chuen asked for both on 2 Nov 2026: results should land in Obsidian within minutes,
not at the 21:40 night review, and merged work should reach GitHub without him.

  * vault_note(): appends one section per finished task to
    <vault>/Calendar/Orchestra/<date>.md and commits only that file in the vault repo.
  * push(): runs only after "merged". It refuses if the branch isn't the default one,
    if a rebase/merge is in progress, or if anything about to go public looks private
    (vault notes, memory, .review, secrets). The repo is public on GitHub.

The task sessions themselves still can't push (guard hook + .git/hooks/pre-push);
only this runner step does, after the runner's own gate passed.
Everything here is best-effort: a failure is logged and never fails the task.
Every vault write and its git add/commit happen under one lock (vaultlock.py, audit 10 Nov 2026 O6).
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import vaultlock  # noqa: E402  the vault-write lock; never imports runner.py

# Paths that must never go public. The repo is public; the vault and memory are not.
PRIVATE_PATHS = re.compile(
    r"^(memory/(?!\.gitkeep$)|\.review/(?!(README|TEMPLATE)\.md$)|Denisse/|Atlas/|Calendar/|Efforts/|Dev/|drafts?/)"
    r"|(^|/)\.env$|(^|/)\.env\.(?!example$)|\.pem$|\.key$|(^|/)(secrets?|credentials?|tokens?)[^/]*$",
    re.I)
# Strings in added lines that look like keys or tokens.
SECRET_TEXT = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AIza[0-9A-Za-z_-]{30,}"
    r"|xox[abp]-[A-Za-z0-9-]{20,}"
    # a PEM header only counts when real key material follows (on the next added line);
    # a bare header in a test fixture is not a key (false alarm on 2 Nov 2026).
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----\s*\n\+?\s*[A-Za-z0-9+/=]{40,})")


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def privacy_problems(repo: Path, upstream: str) -> list[str]:
    """What about origin/<branch>..HEAD shouldn't go public. Empty list = fine."""
    files = _git(repo, "diff", "--name-only", f"{upstream}..HEAD").stdout.split()
    bad = [f for f in files if PRIVATE_PATHS.search(f.replace("\\", "/"))]
    problems = [f"private-looking paths: {', '.join(bad[:8])}"] if bad else []
    diff = _git(repo, "diff", "-U0", f"{upstream}..HEAD").stdout
    added = "\n".join(l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    if SECRET_TEXT.search(added):
        problems.append("an added line looks like a key or token")
    return problems


def push(repo: Path, remote: str = "origin") -> str:
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    default = _git(repo, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD").stdout.strip()
    default = default.split("/", 1)[-1] if default else "master"
    if branch != default:
        return f"not pushed: main checkout is on {branch!r}, not {default!r}"
    gitdir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    if any((gitdir / p).exists() for p in ("MERGE_HEAD", "rebase-merge", "rebase-apply")):
        return "not pushed: a merge or rebase is in progress"
    if _git(repo, "fetch", "-q", remote, branch).returncode != 0:
        return "not pushed: fetch failed (offline?)"
    upstream = f"{remote}/{branch}"
    if _git(repo, "merge-base", "--is-ancestor", upstream, "HEAD").returncode != 0:
        return f"not pushed: {upstream} has commits this checkout doesn't; pull first"
    ahead = _git(repo, "rev-list", "--count", f"{upstream}..HEAD").stdout.strip()
    if ahead == "0":
        return "nothing to push"
    problems = privacy_problems(repo, upstream)
    if problems:
        return "not pushed: " + "; ".join(problems)
    r = _git(repo, "push", remote, f"HEAD:{branch}")
    if r.returncode != 0:
        return "push failed: " + (r.stderr.strip().splitlines() or ["?"])[-1]
    return f"pushed {ahead} commit(s) to {upstream}"


def _sections(report_text: str) -> dict[str, str]:
    """The report's DONE / NOT DONE / ODD / COMMITS blocks."""
    out, key = {}, None
    for line in report_text.splitlines():
        m = re.match(r"^(DONE|NOT DONE|ODD|COMMITS):\s*(.*)$", line)
        if m:
            key = m.group(1)
            out[key] = m.group(2)
        elif re.match(r"^[A-Z ]+:\s", line):
            key = None
        elif key:
            out[key] += "\n" + line
    return {k: v.strip() for k, v in out.items()}


def _asked(task_text: str, limit: int = 300) -> str:
    body = [l for l in task_text.splitlines()[1:]
            if l.strip() and not re.match(r"^(Queue|Priority|Weight|Kind|Lane|Written):", l)]
    para = []
    for l in body:
        if l.startswith("#"):
            if para:
                break
            continue
        para.append(l.strip())
    s = " ".join(para)
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _tokens(tokens: dict | None) -> str:
    """Telemetry, not an allowance: what the run used, as the lane reported it."""
    if not tokens:
        return "tokens n/a"
    names = (("in", "in"), ("out", "out"), ("cache_read", "cache-read"), ("cache_write", "cache-write"))
    return "tokens " + " ".join(f"{label} {tokens[k] if tokens.get(k) is not None else '?'}" for k, label in names)


def _day_note(vault: Path, day: str) -> Path:
    """<vault>/Calendar/Orchestra/<day>.md, created with its frontmatter if missing. Under the lock."""
    note = vault / "Calendar" / "Orchestra" / f"{day}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    if not note.exists():
        note.write_text(
            "---\n"
            f"name: Orchestra {day}\n"
            f"description: What Orchestra's unattended Claude/Codex tasks did on {day}, one section per task.\n"
            f"updated: {day}\n"
            "type: dated\n"
            "tags: [orchestra]\n"
            "protection: append\n"
            "---\n"
            f"# Orchestra {day}\n\n"
            f"Written by the runner as each task finishes. See also [[Handoff-{day}]].\n",
            encoding="utf-8")
    return note


def _append_section(vault: Path, section: str, message: str) -> Path:
    """Append one section to today's day note and commit only that file, all under the vault
    lock. The section goes on with one append-mode write, so a section another tick wrote a
    moment ago is never read-and-rewritten away. Only the frontmatter `updated:` line is ever
    rewritten, and only when it isn't today's date already (the note is created with it)."""
    day = dt.date.today().isoformat()
    with vaultlock.VaultLock():
        note = _day_note(vault, day)
        raw = note.read_bytes()
        nl = "\r\n" if b"\r\n" in raw else "\n"  # the note's own line ends, kept as they are
        text = raw.decode("utf-8")
        m = re.search(r"^updated: [^\r\n]*", text, flags=re.M)
        if m and m.group(0) != f"updated: {day}":
            raw = (text[:m.start()] + f"updated: {day}" + text[m.end():]).encode("utf-8")
            vaultlock.write_atomic(note, raw)
        lead = "" if raw.endswith(b"\n") else nl
        with open(note, "ab") as f:  # one append: nothing already in the note is rewritten
            f.write((lead + section.replace("\r\n", "\n").replace("\n", nl)).encode("utf-8"))
        rel = note.relative_to(vault).as_posix()
        if (vault / ".git").exists():  # best-effort, and inside the lock: one tick at a time on the index
            _git(vault, "add", "--", rel)
            _git(vault, "commit", "-q", "-m", message, "--", rel)
    return note


def vault_note(vault: Path, *, slug: str, title: str, result: str, lane: str, seconds: int,
               spent: str, merge: str = "", pushed: str = "", why: str = "",
               task_text: str = "", report_text: str = "", report_path: str = "",
               prompt: str = "", tokens: dict | None = None) -> Path:
    sec = _sections(report_text)
    status = result + (f", {merge}" if merge else "") + (f", {pushed}" if pushed else "")
    lines = [f"\n## {dt.datetime.now():%H:%M} {title}",
             f"- **{status}** · lane {lane} · {max(1, round(seconds / 60))} min · {spent}",
             f"- Task: `{slug}` · prompt `{prompt or '?'}` · {_tokens(tokens)}"]
    if why:
        lines.append(f"- Why it stopped: {why}")
    if task_text:
        lines.append(f"- Asked: {_asked(task_text)}")
    for k in ("DONE", "NOT DONE", "ODD"):
        if sec.get(k) and sec[k].lstrip("- ").rstrip(". ").lower() not in ("none", "", "nothing", "n/a"):
            lines.append(f"\n**{k.capitalize()}**\n{sec[k]}")
    if report_path:
        lines.append(f"\nFull report: `{report_path}`")
    return _append_section(vault, "\n".join(lines) + "\n", f"Orchestra: {slug} {result}")


# ---------------------------------------------------------------- hot.md "Waiting on Chuen"
# Chuen, 2 Nov 2026: a draft task, a parked task or a "merge by hand" shouldn't wait hours for
# the 21:40 night review to put it in hot.md. Orchestra owns only the lines between these
# markers; every other line of hot.md (his, the night review's) is left byte-for-byte alone.
WAIT_OPEN, WAIT_CLOSE = "<!-- orchestra:waiting -->", "<!-- /orchestra:waiting -->"
WAIT_HEADING = "## Waiting on Chuen"


def waiting_lines(*, queue_dir: Path, parked_dir: Path, state: dict, now: dt.datetime,
                  drop_landed=None, merge_waiting=None) -> list[str]:
    """What Orchestra has waiting on Chuen, one markdown bullet each, from the runner's own files:
    draft tasks to approve, parked tasks (first line of the why file), PASSes that didn't merge,
    and lanes limited until a time. `drop_landed(state)` (runner.drop_landed) first marks the
    PASSes whose branch has since landed on master; `merge_waiting(note)` (runner.merge_waiting)
    says which merge notes still wait. Without them: every note but merged / no commits waits."""
    if drop_landed:
        drop_landed(state)
    waits = merge_waiting or (lambda m: bool(m) and m not in ("merged", "no commits"))
    def title(p: Path) -> str:
        try:
            first = next((l for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
                          if l.startswith("# ")), "")
        except OSError:
            first = ""
        return first[2:].strip() or p.stem

    out = []
    for p in sorted(queue_dir.glob("*.md")) if queue_dir.is_dir() else []:
        try:
            head = p.read_text(encoding="utf-8", errors="replace").splitlines()[1:20]
        except OSError:
            continue
        q = next((m.group(1).strip() for l in head if (m := re.match(r"^Queue:\s*(.*)$", l, re.I))), "")
        if q.lower() == "draft":
            out.append(f"- Approve draft task \"{title(p)}\": change `Queue: draft` → `Queue: auto` in `{p}`")
    for why in sorted(parked_dir.glob("*.why.txt")) if parked_dir.is_dir() else []:
        slug = why.name[: -len(".why.txt")]
        try:
            first = (why.read_text(encoding="utf-8", errors="replace").splitlines() or [""])[0]
        except OSError:
            first = ""
        reason = first.split(" ", 1)[-1].strip() if " " in first else first.strip()
        out.append(f"- Parked \"{title(parked_dir / (slug + '.md'))}\" (`{slug}`): {reason or '(no reason given)'}")
    for slug, ts in sorted((state.get("tasks") or {}).items()):
        m = ts.get("merge", "")
        if waits(m):
            out.append(f"- PASS not merged: `review/{slug}` — {m}")
    for lane, info in sorted((state.get("lanes") or {}).items()):
        until = (info or {}).get("limited_until")
        try:
            when = dt.datetime.fromisoformat(until) if until else None
        except ValueError:
            continue
        if when and when > now:
            out.append(f"- Lane {lane} limited until {when:%Y-%m-%d %H:%M}")
    return out


def _render_hot(text: str, lines: list[str]) -> str:
    """hot.md with the waiting block set to `lines`. Only the block's own lines change; the
    markers are created once (under the heading, or with a new heading at the end)."""
    nl = "\r\n" if "\r\n" in text else "\n"
    block = nl.join([WAIT_OPEN, *lines, WAIT_CLOSE])
    a, b = text.find(WAIT_OPEN), text.find(WAIT_CLOSE)
    if a != -1 and b > a:
        return text[:a] + block + text[b + len(WAIT_CLOSE):]
    m = re.search(r"^" + re.escape(WAIT_HEADING) + r"[ \t]*(\r?\n|\Z)", text, re.M)
    if m:
        end = m.end()
        sep = "" if m.group(1) else nl
        return text[:end] + sep + block + nl + text[end:]
    pre = "" if not text or text.endswith("\n") else nl
    gap = nl if text else ""
    return text + pre + gap + WAIT_HEADING + nl + block + nl


def update_waiting(vault: Path, lines: list[str]) -> bool:
    """Rewrite hot.md's Orchestra waiting block; commit only hot.md, only when the block changed.
    Returns True when hot.md was changed. hot.md is Chuen's own working note: under the vault
    lock it is read, the new text built, and written (temp file + os.replace, bytes so CRLF stays
    exactly as it was) only if it is still what was read. If he saved it in between, his text is
    left alone and this returns False; the next tick tries again. A lock held past the wait time
    is the same: False, next tick."""
    hot = vault / "hot.md"
    try:
        with vaultlock.VaultLock():
            if not vaultlock.replace_if_unchanged(hot, lambda old: _render_hot(old, lines)):
                return False
            if (vault / ".git").exists():  # best-effort, inside the lock
                _git(vault, "add", "--", "hot.md")
                _git(vault, "commit", "-q", "-m", "Orchestra: waiting list", "--", "hot.md")
    except TimeoutError:
        return False
    return True


def exam_note(vault: Path, line: str) -> Path:
    """One line for the daily exam run (orchestra/exams/daily.py): agents and pass rates."""
    return _append_section(vault, f"\n## {dt.datetime.now():%H:%M} Exams\n- {line}\n", "Orchestra: daily exams")
