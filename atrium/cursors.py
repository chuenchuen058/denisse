"""Agent cursors for the live Atrium page: where each running agent is, and who is about to collide.

For every running slot (snapshot's "running" table, nothing collected twice) this reads the
files its worktree has touched so far: committed since it branched from base_ref, changed but
uncommitted, and untracked. Read commands only (git diff / ls-files / merge-base, with
GIT_OPTIONAL_LOCKS=0 so git never even refreshes the index) and an os.stat per file for "the
one it touched last". Results are cached per worktree for the live page's refresh_s, so the
page's existing update loop is the only loop. Two agents touching the same file is an overlap:
one warning per shared file. Paths under the runner's conflict_ignore_prefixes (the same list
orchestra/conflicts.py ignores) and the patterns in atrium/config.json "live" -> "cursors" ->
"ignore" never count. Every value: atrium/config.json "live" -> "cursors".
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
import time
from pathlib import Path

from . import conf, snapshot

_cache: dict[str, tuple[float, list[dict]]] = {}


def _c() -> dict:
    return conf.section("live")["cursors"]


def worktrees() -> Path:
    """The folder the runner puts one worktree per task in (orchestra/runner.py WORKTREES)."""
    repo = snapshot.REPO
    return Path(_c()["worktrees"].format(repo_parent=repo.parent, repo_name=repo.name))


def _env() -> dict:
    """No inherited GIT_* (a lane's GIT_DIR would point every call at another repo); no locks."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(_c()["git_env"])
    return env


def _git(wt: Path, *args: str) -> list[str] | None:
    try:
        r = subprocess.run(["git", *args], cwd=wt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=_c()["git_timeout_s"], env=_env())
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.splitlines() if r.returncode == 0 else None


def ignore_rules() -> tuple[tuple[str, ...], list[str]]:
    """(the runner's ignore prefixes, this page's own glob patterns)."""
    c = _c()
    prefixes = tuple(snapshot._config().get(c["runner_ignore_key"], [])) if c["use_runner_ignores"] else ()
    return prefixes, list(c["ignore"])


def ignored(path: str, rules: tuple[tuple[str, ...], list[str]]) -> bool:
    prefixes, globs = rules
    name = path.rsplit("/", 1)[-1]
    return path.startswith(prefixes) or any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in globs)


def touched(wt: Path) -> list[dict]:
    """[{path, mtime}] the worktree has touched since it branched, ignored paths left out.
    Empty when there is no worktree (yet) or git cannot read it."""
    if not (wt / ".git").exists():
        return []
    c = _c()
    base = _git(wt, "merge-base", "HEAD", c["base_ref"]) or []
    names: set[str] = set()
    for args in (["diff", "--name-only", base[0] if base else "HEAD"],
                 ["ls-files", "--others", "--exclude-standard"]):
        out = _git(wt, "-c", "core.quotepath=false", *args)
        names |= {n.strip() for n in out or [] if n.strip()}
    rules = ignore_rules()
    rows = []
    for n in sorted(names):
        n = n.replace("\\", "/")
        if ignored(n, rules):
            continue
        try:
            m = (wt / n).stat().st_mtime
        except OSError:
            m = 0.0  # deleted in the worktree: still touched
        rows.append({"path": n, "mtime": m})
    return rows


def touched_cached(wt: Path, ttl: float) -> list[dict]:
    key = str(wt)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    rows = touched(wt)
    _cache[key] = (time.monotonic(), rows)
    return rows


def collect(running: list[dict], agents: list[dict], ttl: float, words: dict) -> dict:
    """{cursors, files, overlaps} for the page. One cursor per running task (idle slots give
    none); files = every touched path once with who is in it, shared ones first; overlaps =
    one per shared file, with the page's warning sentence."""
    c = _c()
    by_lane = {a["lane"]: a for a in agents}
    busy = [r for r in running if r.get("task")]
    per_lane = {}
    for r in busy:
        per_lane[r["lane"]] = per_lane.get(r["lane"], 0) + 1
    root = worktrees()
    cursors: list[dict] = []
    who_in: dict[str, list[str]] = {}
    newest: dict[str, float] = {}
    for r in busy:
        a = by_lane.get(r["lane"], {})
        label = a.get("label", r["lane"])
        if per_lane[r["lane"]] > 1:
            label = words["cursor_slot"].format(label=label, slot=r["slot"])
        rows = touched_cached(root / r["id"], ttl)
        cid = f"{r['lane']}:{r['slot']}"
        last = max(rows, key=lambda f: f["mtime"])["path"] if rows else ""
        cursors.append({"id": cid, "lane": r["lane"], "label": label, "task_id": r["id"], "task": r["task"],
                        "file": last, "files": len(rows)})
        for f in rows:
            who_in.setdefault(f["path"], []).append(cid)
            newest[f["path"]] = max(newest.get(f["path"], 0.0), f["mtime"])
    labels = {k["id"]: k["label"] for k in cursors}
    overlaps = [{"path": p, "who": ids,
                 "text": words["both_in"].format(who=words["both_join"].join(labels[i] for i in ids), path=p)}
                for p, ids in sorted(who_in.items()) if len(ids) > 1]
    shared = {o["path"] for o in overlaps}
    order = sorted(who_in, key=lambda p: (p not in shared, -newest[p], p))
    shown = order[:c["max_files"]]
    for k in cursors:  # a current file that is not on the list has nowhere to point
        if k["file"] not in shown:
            k["file"] = ""
    files = [{"path": p, "who": who_in[p], "shared": p in shared} for p in shown]
    return {"cursors": cursors, "files": files, "overlaps": overlaps,
            "more_files": words["more_files"].format(n=len(order) - len(shown)) if len(order) > len(shown) else "",
            "cursor_cfg": {k: c[k] for k in c["page"]}}
