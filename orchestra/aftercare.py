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
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path

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


def vault_note(vault: Path, *, slug: str, title: str, result: str, lane: str, seconds: int,
               spent: str, merge: str = "", pushed: str = "", why: str = "",
               task_text: str = "", report_text: str = "", report_path: str = "") -> Path:
    day = dt.date.today().isoformat()
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
    sec = _sections(report_text)
    status = result + (f", {merge}" if merge else "") + (f", {pushed}" if pushed else "")
    lines = [f"\n## {dt.datetime.now():%H:%M} {title}",
             f"- **{status}** · lane {lane} · {max(1, round(seconds / 60))} min · {spent}",
             f"- Task: `{slug}`"]
    if why:
        lines.append(f"- Why it stopped: {why}")
    if task_text:
        lines.append(f"- Asked: {_asked(task_text)}")
    for k in ("DONE", "NOT DONE", "ODD"):
        if sec.get(k) and sec[k].lstrip("- ").rstrip(". ").lower() not in ("none", "", "nothing", "n/a"):
            lines.append(f"\n**{k.capitalize()}**\n{sec[k]}")
    if report_path:
        lines.append(f"\nFull report: `{report_path}`")
    text = note.read_text(encoding="utf-8")
    text = re.sub(r"^updated: .*$", f"updated: {day}", text, count=1, flags=re.M)
    note.write_text(text.rstrip("\n") + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    rel = note.relative_to(vault).as_posix()
    if (vault / ".git").exists():
        _git(vault, "add", "--", rel)
        _git(vault, "commit", "-q", "-m", f"Orchestra: {slug} {result}", "--", rel)
    return note
