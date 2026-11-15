"""Which branch is the default one, and is the main checkout on it?

The runner branches every task from the main checkout's HEAD and fast-forwards whatever is
checked out. If Chuen (or one of his parallel sessions) ran `git switch` there, tasks would
start from that branch and PASSes would be merged onto it, with the log still reading
`merged` (audit 10 Nov 2026, O1). So before dispatch and before every fast-forward the runner
asks `off_default()`; aftercare.push() uses the same lookup.

The default branch is the remote's HEAD (`git symbolic-ref refs/remotes/<default_remote>/HEAD`),
else config `default_branch` when that branch exists. Neither: unknown, and nothing is blocked
(a repo with no such branch has nothing to protect). Never imports runner.py.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

STATE_NAME = "default_branch.json"  # in .review/: what was last logged, so it is logged once per change


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def remote_of(cfg: dict | None) -> str:
    return str((cfg or {}).get("default_remote") or "origin")


def default_branch(repo: Path, cfg: dict | None = None) -> str | None:
    """The default branch's short name, or None when it can't be told."""
    remote = remote_of(cfg)
    r = _git(repo, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    name = r.stdout.strip()
    if r.returncode == 0 and name.startswith(remote + "/"):
        return name[len(remote) + 1:]
    name = str((cfg or {}).get("default_branch") or "").strip()
    if name and _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
        return name
    return None


def current_branch(repo: Path) -> str | None:
    """The branch the checkout at `repo` is on; None on a detached HEAD."""
    r = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def status(repo: Path, cfg: dict | None = None) -> tuple[str | None, str | None]:
    """(default branch, current branch) for the checkout at `repo`."""
    return default_branch(repo, cfg), current_branch(repo)


def describe(current: str | None) -> str:
    return f"branch {current!r}" if current else "a detached HEAD"


def off_default(repo: Path, cfg: dict | None = None) -> str | None:
    """Why nothing may be dispatched or merged (the checkout is off the default branch), or None."""
    default, current = status(repo, cfg)
    if default is None or current == default:
        return None
    return f"the main checkout is on {describe(current)}, not the default branch {default!r}"


def park_reason(why: str, slug: str) -> str:
    """The integrate() outcome for a PASS that can't be merged while the checkout is off."""
    return (f"parked: {why}; nothing merged. Once the main checkout is back on the default branch: "
            f"requeue.py {slug} --merge-only")


def dispatch_block(repo: Path, review: Path, cfg: dict | None, log) -> str | None:
    """off_default(), with one log line when the state changes (not one per tick): the last
    logged state is kept in review/STATE_NAME, like the runner's battery_pause() power file."""
    why = off_default(repo, cfg)
    f = review / STATE_NAME
    try:
        last = json.loads(f.read_text(encoding="utf-8")).get("why")
    except (OSError, ValueError, AttributeError):
        last = None
    if why and why != last:
        log(f"OFF DEFAULT BRANCH: no new dispatch and no merge: {why}")
        review.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"why": why, "logged": dt.datetime.now().isoformat()}), encoding="utf-8")
    elif not why and f.exists():
        f.unlink(missing_ok=True)
        log("DEFAULT BRANCH: the main checkout is back on the default branch; dispatching again")
    return why
