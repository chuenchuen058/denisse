"""Conflict-aware dispatch and merge follow-ups for the Orchestra runner.

On 2 Nov 2026, with 3 Claude slots + Codex running at once, six tasks PASSed and then
could not be merged ("merge conflict; merge by hand", or the combined tree failing because
both sides had changed eval/cases.py, orchestra/README.md, orchestra/config.json ...).
Each one needed a person to notice and queue a merge-only task by hand. This module does
three things, each through a small hook in runner.py:

  * predict  -- the files a task will probably touch: paths named in its body (backticked,
                or bare `orchestra/x.py`), plus a map learned from finished tasks (title
                words -> files their merged branch changed, .review/<conflict_map_file>),
                plus, for a *running* task, what its worktree has actually changed so far
                (one `git diff --name-only` per running task per tick).
  * dispatch -- runner.pick() hands the sorted runnable list here. A candidate whose
                predicted files overlap a running task's files on a *hot* file
                (conflict_hot_files) is held while another candidate without an overlap
                exists. Never starved: after conflict_max_holds holds it runs anyway.
                Every hold is one log line: "held <slug>: overlaps <other> on <file>".
  * recover  -- a PASS that hits a merge conflict, or whose combined tree fails while both
                sides changed the same files, gets ONE follow-up task <slug><suffix>
                (orchestra/merge_followup_prompt.md; header Merge-Of: <slug>): merge
                review/<slug> onto current master keeping both sides, re-run the original
                gate. If that follow-up fails too it parks like any task and a person looks.

Every value (hot files, hold limit, follow-up lane/priority/suffix, stop words, map
threshold, ...) is in orchestra/config.json under conflict_*.

    .venv\\Scripts\\python.exe orchestra\\conflicts.py --predict .review/queue/<task>.md
    .venv\\Scripts\\python.exe orchestra\\conflicts.py --bootstrap   # learn from past merges
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner as R  # noqa: E402  (runner imports this module lazily, inside its functions)

FOLLOWUP_TEMPLATE = R.HERE / "merge_followup_prompt.md"


def cfg(key: str):
    """A conflict_* value; runner.load_config() fills every key from config_defaults.json."""
    return R.C[key]


def ignored(path: str) -> bool:
    return path.startswith(tuple(cfg("conflict_ignore_prefixes")))


# ---------------------------------------------------------------- predict: paths in a task body

_EXT = r"\.[A-Za-z][\w-]*"
_BARE = re.compile(r"^[\w.-]+" + _EXT + r"$")
_PATHLIKE = re.compile(r"(?<![\w/\\.@-])((?:[\w.-]+[/\\])+[\w.-]+" + _EXT + r")")


def _norm(s: str) -> str:
    s = s.strip().strip("\"'").replace("\\", "/").rstrip(".,:;)")
    while s.startswith("./"):
        s = s[2:]
    return s


def _plausible(p: str) -> bool:
    if not p or p.endswith("/") or ignored(p) or p.startswith("/") or ":" in p:
        return False
    return not any(ch in p for ch in "*?<>{}$ ")


def paths_in_text(text: str, known: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """Repo-relative files a task body names: backticked paths and bare file names (resolved
    against `known`, the repo's tracked files, when given), plus unbackticked `dir/file.ext`
    tokens. URLs, ignored prefixes (.review/, .venv/ ...), globs and directories are left out."""
    text = re.sub(r"\w+://\S+", " ", text)
    paths, bare = set(), set()
    for span in re.findall(r"`([^`\n]+)`", text):
        s = _norm(span)
        if "/" in s and _plausible(s):
            paths.add(s)
        elif _BARE.match(s):
            bare.add(s)
    for m in _PATHLIKE.finditer(text):
        s = _norm(m.group(1))
        if _plausible(s):
            paths.add(s)
    if known:
        # a slash path with no extension must be a real file (not a branch like review/<slug>)
        paths = {p for p in paths if p in known or "." in p.rsplit("/", 1)[-1]}
        for b in bare:
            hits = {k for k in known if k.rsplit("/", 1)[-1] == b}
            paths |= hits or {b}
    else:
        paths |= bare
    return paths


def known_files() -> set[str]:
    """The repo's tracked files (one `git ls-files`), for resolving bare names."""
    r = R.git("ls-files", check=False)
    return set(r.stdout.split()) if r.returncode == 0 else set()


# ---------------------------------------------------------------- predict: the learned map

def words_of(*texts: str) -> set[str]:
    """Title (or slug) words the map is keyed by: lower case, at least conflict_min_word_len
    letters, no digits, not in conflict_stop_words."""
    stop, n = set(cfg("conflict_stop_words")), int(cfg("conflict_min_word_len"))
    out = set()
    for t in texts:
        for w in re.split(r"[^a-z0-9]+", (t or "").lower()):
            if len(w) >= n and w[0].isalpha() and not any(c.isdigit() for c in w) and w not in stop:
                out.add(w)
    return out


def map_file() -> Path:
    return R.REVIEW / cfg("conflict_map_file")


def load_map() -> dict:
    """{"words": {word: {"n": times seen, "files": {file: times touched}}}}. On first use the map
    is seeded from master's own `orchestra: merge review/<slug>` commits (slug words -> files)."""
    try:
        m = json.loads(map_file().read_text(encoding="utf-8"))
        if isinstance(m, dict) and isinstance(m.get("words"), dict):
            return m
    except (OSError, ValueError):
        pass
    m = {"words": {}}
    try:
        bootstrap(m)
    except Exception as e:  # a bad history read must not stop dispatch
        R.log(f"conflicts: bootstrap skipped: {e!r}")
    save_map(m)
    return m


def save_map(m: dict) -> None:
    R.REVIEW.mkdir(parents=True, exist_ok=True)
    m["updated"] = R.now().isoformat()
    tmp = map_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, map_file())


def learn(m: dict, words: set[str], files: set[str]) -> None:
    files = {f for f in files if not ignored(f)}
    if not words or not files:
        return
    for w in words:
        e = m["words"].setdefault(w, {"n": 0, "files": {}})
        e["n"] += 1
        for f in files:
            e["files"][f] = e["files"].get(f, 0) + 1


def predicted_from_map(m: dict, words: set[str], threshold: float | None = None) -> set[str]:
    """Files the words point at: each word votes p(file | word) = touched / seen; a file whose
    votes add up to conflict_map_threshold is predicted (at 1.0: one sure word, or two
    half-sure ones)."""
    threshold = float(cfg("conflict_map_threshold") if threshold is None else threshold)
    score: dict[str, float] = {}
    for w in words:
        e = m.get("words", {}).get(w)
        if not e or not e.get("n"):
            continue
        for f, c in e.get("files", {}).items():
            score[f] = score.get(f, 0.0) + c / e["n"]
    return {f for f, s in score.items() if s >= threshold}


def bootstrap(m: dict, limit: int | None = None) -> int:
    """Learn from master's past `orchestra: merge review/<slug>` commits (the newest
    conflict_bootstrap_merges of them). Returns merges read."""
    limit = int(cfg("conflict_bootstrap_merges") if limit is None else limit)
    r = R.git("log", "--merges", f"-{limit}", "--format=%H %s", check=False)
    n = 0
    for line in r.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        mm = re.search(r"merge review/(\S+)", subject)
        if not mm:
            continue
        files = set(R.git("diff", "--name-only", f"{sha}^1", sha, check=False).stdout.split())
        learn(m, words_of(mm.group(1)), files)
        n += 1
    return n


# ---------------------------------------------------------------- predict: per task

def predict(task: "R.Task", known: set[str], m: dict) -> set[str]:
    try:
        text = task.path.read_text(encoding="utf-8")
    except OSError:
        text = task.title
    return paths_in_text(text, known) | predicted_from_map(m, words_of(task.title, task.slug))


def worktree_files(slug: str) -> set[str]:
    """What a running task's worktree has changed so far (committed, staged or not) against
    the point it branched from. Empty if there is no worktree yet."""
    wt = R.WORKTREES / slug
    if not (wt / ".git").exists():
        return set()
    master = R.git("rev-parse", "HEAD", check=False).stdout.strip()
    base = R.git("merge-base", "HEAD", master, cwd=wt, check=False).stdout.strip() if master else ""
    r = R.git("diff", "--name-only", base or "HEAD", cwd=wt, check=False)
    return {f for f in r.stdout.split() if not ignored(f)} if r.returncode == 0 else set()


def running_files(slug: str, known: set[str], m: dict) -> set[str]:
    """A running task's files: what it has changed plus what it was predicted to change."""
    files = worktree_files(slug)
    path = R.QUEUE / f"{slug}.md"
    if path.exists():
        try:
            files |= predict(R.parse_task(path), known, m)
        except Exception:
            pass
    return files


# ---------------------------------------------------------------- dispatch

def hot_patterns() -> list[str]:
    return list(cfg("conflict_hot_files"))


def is_hot(path: str, patterns: list[str]) -> bool:
    """A pattern with a slash matches the whole path; one without matches the file name
    ("README.md" means every README)."""
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, p) if "/" in p else fnmatch.fnmatch(name, p) for p in patterns)


def overlap(mine: set[str], running: dict[str, set[str]], hot: list[str]) -> tuple[str, list[str]] | None:
    """(other slug, hot files both touch) for the first running task this one collides with."""
    for slug, theirs in sorted(running.items()):
        both = sorted(f for f in mine & theirs if is_hot(f, hot))
        if both:
            return slug, both
    return None


def choose(cands: list, pred: dict[str, set[str]], running: dict[str, set[str]], hot: list[str],
           holds: dict[str, int], max_holds: int):
    """Pure dispatch rule. Returns (chosen task or None, held tasks passed over, notes).
    Candidates are in priority order; the first without a hot overlap wins. One that has been
    held max_holds times runs anyway. If every candidate overlaps, the first runs (no holds)."""
    held, notes = [], []
    for t in cands:
        clash = overlap(pred.get(t.slug, set()), running, hot)
        if clash is None:
            return t, held, notes
        if holds.get(t.slug, 0) >= max_holds:
            notes.append(f"{t.slug} runs anyway after {holds[t.slug]} holds (overlaps {clash[0]} on {', '.join(clash[1])})")
            return t, held, notes
        held.append((t, clash))
    if cands:  # everything overlaps something: never starve
        t = cands[0]
        notes.append(f"{t.slug} runs although it overlaps {held[0][1][0]} on {', '.join(held[0][1][1])}: no alternative")
        return t, [], notes
    return None, [], notes


def pick(runnable: list, state: dict, quiet: bool = False):
    """runner.pick()'s hook: `runnable` is already sorted by priority. With nothing running the
    answer is runnable[0] and no git is called, so single-lane behaviour is unchanged. `quiet`
    (a dry run) chooses the same way but records and logs nothing."""
    if not runnable:
        return None
    others = sorted(R.running_tasks())
    cands = [t for t in runnable if t.slug not in others]
    if not others or not cands:
        return runnable[0]
    known, m, hot = known_files(), load_map(), hot_patterns()
    max_holds = int(cfg("conflict_max_holds"))
    pred = {t.slug: predict(t, known, m) for t in cands}
    running = {s: running_files(s, known, m) for s in others}
    holds = {t.slug: int(state["tasks"].get(t.slug, {}).get("conflict_holds", 0)) for t in cands}
    chosen, held, notes = choose(cands, pred, running, hot, holds, max_holds)
    if quiet:
        return chosen
    for t, (other, files) in held:
        ts = state["tasks"].setdefault(t.slug, {"attempts": 0, "history": []})
        ts["conflict_holds"] = holds[t.slug] + 1
        R.log(f"held {t.slug}: overlaps {other} on {', '.join(files)} (hold {ts['conflict_holds']}/{max_holds})")
    for n in notes:
        R.log(f"dispatch: {n}")
    if chosen is not None:
        state["tasks"].get(chosen.slug, {}).pop("conflict_holds", None)
    return chosen


# ---------------------------------------------------------------- recover

def both_sides(branch: str) -> list[str]:
    """Files changed both on master since the branch left it and on the branch."""
    base = R.git("merge-base", "HEAD", branch, check=False).stdout.strip()
    if not base:
        return []
    ours = set(R.git("diff", "--name-only", base, "HEAD", check=False).stdout.split())
    theirs = set(R.git("diff", "--name-only", base, branch, check=False).stdout.split())
    return sorted(ours & theirs)


def conflict_reason(merge: str, branch: str) -> str | None:
    """Why a failed merge outcome is a conflict between branches (and worth a merge follow-up),
    or None: a plain red combined tree, a dirty checkout, master moving, etc. are not."""
    if merge.startswith("merge conflict"):
        return "merge conflict with current master"
    if merge.startswith("parked: the combined tree"):
        both = both_sides(branch)
        if both:
            shown = int(cfg("conflict_both_sides_shown"))
            return f"the combined tree fails and both sides changed {', '.join(both[:shown])}"
    return None


def followup_slug(slug: str) -> str:
    return f"{slug}{cfg('conflict_followup_suffix')}"


def followup_exists(slug: str) -> bool:
    name = f"{followup_slug(slug)}.md"
    if (R.QUEUE / name).exists() or (R.PARKED / name).exists():
        return True
    return R.ARCHIVE.exists() and any(R.ARCHIVE.glob(f"*/{name}"))


def followup_text(task: "R.Task", branch: str, why: str, touched: set[str]) -> str:
    fmt = lambda files: ", ".join(f"`{f}`" for f in sorted(files)) or "none found"
    return FOLLOWUP_TEMPLATE.read_text(encoding="utf-8").format(
        slug=task.slug, branch=branch, weight=task.weight, lane=cfg("conflict_followup_lane"),
        priority=cfg("conflict_followup_priority"), written=R.now().strftime("%Y-%m-%dT%H:%M"),
        title=task.title.replace('"', "'"), why=why, both=fmt(both_sides(branch)), touched=fmt(touched),
        gate=task.gate.strip() or "(the original task had no gate)")


def queue_followup(task: "R.Task", branch: str, why: str, state: dict, touched: set[str]) -> str:
    """Write .review/queue/<slug><suffix>.md once. Returns a short note for the log and the
    merge/park text: 'follow-up <slug><suffix> queued', or why not."""
    if task.fields.get("merge-of"):
        return "this was already the merge follow-up; a person must look"
    if not cfg("conflict_followup"):
        return "merge follow-ups are off (conflict_followup)"
    slug = followup_slug(task.slug)
    ts = state["tasks"].setdefault(task.slug, {"attempts": 0, "history": []})
    if ts.get("merge_followup") or followup_exists(task.slug):
        return f"follow-up {slug} already exists; a person must look"
    text = followup_text(task, branch, why, touched)
    R.QUEUE.mkdir(parents=True, exist_ok=True)
    tmp = R.QUEUE / f"{slug}.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, R.QUEUE / f"{slug}.md")  # the runner never sees a half-written task
    ts["merge_followup"] = slug
    R.log(f"FOLLOW-UP {slug} queued: {why}")
    return f"follow-up {slug} queued"


def close_original(orig: str, by: str, state: dict) -> None:
    """The follow-up merged: the original's work is on master. Mark it, and move a parked
    original (a red combined tree parks it) to today's archive so it stops waiting on a person."""
    if orig in state["tasks"]:
        state["tasks"][orig]["merge"] = f"merged by {by}"
    parked = R.PARKED / f"{orig}.md"
    if parked.exists():
        day = R.ARCHIVE / R.now().date().isoformat()
        R.move(parked, day)
        for extra in (f"{orig}.report.md", f"{orig}.why.txt"):
            if (R.PARKED / extra).exists():
                R.move(R.PARKED / extra, day)
        R.log(f"{orig}: archived from parked, merged by {by}")


def after_integrate(task: "R.Task", branch: str, merge: str, state: dict, touched: set[str]) -> str:
    """runner's hook, right after integrate(). Learns from a merged task, queues the merge
    follow-up after a conflict, and closes the original when its follow-up lands.
    Returns the merge outcome text, extended with what was done."""
    if merge == "merged":
        orig = task.fields.get("merge-of")
        if orig:
            close_original(orig, task.slug, state)
        elif touched:
            m = load_map()
            learn(m, words_of(task.title, task.slug), touched)
            save_map(m)
        return merge
    why = conflict_reason(merge, branch)
    if not why:
        return merge
    note = queue_followup(task, branch, why, state, touched)
    return f"{merge}; {note}"


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--predict", metavar="TASK.md", help="print the files this task is predicted to touch")
    ap.add_argument("--bootstrap", action="store_true", help="rebuild the learned map from master's merges")
    a = ap.parse_args(argv)
    if a.bootstrap:
        m = {"words": {}}
        n = bootstrap(m)
        save_map(m)
        print(f"learned from {n} merge(s): {len(m['words'])} words -> {map_file()}")
    if a.predict:
        task = R.parse_task(Path(a.predict))
        for f in sorted(predict(task, known_files(), load_map())):
            print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
