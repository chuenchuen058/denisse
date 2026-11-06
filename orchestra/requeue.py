"""Put a parked task back in the queue, so nobody edits state.json by hand.

    .venv\\Scripts\\python.exe orchestra\\requeue.py <slug> [--note "why"]

Moves .review/parked/<slug>.md back to .review/queue/ with a `Re-queued <when>: <note>`
line appended (new content, so the runner treats it as a fresh task), moves its report
to .review/reports/<slug>.attempt-parked.md, deletes <slug>.why.txt, and resets the
task's attempts and last_end in state.json through the runner's own load/save (which
merge with a lane runner saving at the same time). Refuses while the task is running.

    .venv\\Scripts\\python.exe orchestra\\requeue.py <slug> --merge-only [--note "why"]

--merge-only is for a task whose work is done on branch review/<slug> but never reached
master (merge conflict, integration reject). It takes the task from .review/parked/ or,
failing that, the newest .review/archive/<day>/ (archive copies stay where they are), sets
`Priority: urgent`, and inserts a paragraph (config: merge_only_mark, merge_only_paragraph)
before `## Gate`. The runner sees that mark and merges the existing branch itself (gate,
reviewer, combined-tree integration) without running a lane; a missing branch or a merge
that can't run parks the task. Exits 2 if the branch doesn't exist. Running it again while
that task is still queued is a no-op.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner as R  # noqa: E402


class RequeueError(Exception):
    pass


class NoBranchError(RequeueError):
    """--merge-only on a slug with no review/<slug> branch: exit 2, nothing to merge."""


MERGE_MARK = R.C["merge_only_mark"]


def running_slugs() -> dict[str, str]:
    """slug -> lock file name, for every task a runner says it is running."""
    out = {}
    for lf in sorted(R.REVIEW.glob("runner*.lock")):
        try:
            slug = json.loads(lf.read_text(encoding="utf-8")).get("task")
        except (OSError, ValueError):
            continue
        if slug:
            out[slug] = lf.name
    return out


def _free_name(directory: Path, stem: str) -> Path:
    """<stem>.md, or <stem>-2.md, -3 ... so an earlier re-queue's report is never overwritten."""
    path, n = directory / f"{stem}.md", 2
    while path.exists():
        path, n = directory / f"{stem}-{n}.md", n + 1
    return path


def _reset_state(slug: str) -> None:
    # Through the runner's save_state with a base set, so a lane runner that saved
    # meanwhile keeps its changes and only this task's entry is ours.
    state = R.load_state()
    R._BASE = json.loads(json.dumps(state))
    try:
        ts = state["tasks"].get(slug)
        if ts is not None:
            ts.update(attempts=0, last_end=None)
            R.save_state(state)
    finally:
        R._BASE = None


def _move_parked_leftovers(slug: str) -> Path | None:
    """Parked report -> reports/<slug>.attempt-parked[-n].md; drop the why file."""
    report = R.PARKED / f"{slug}.report.md"
    moved = None
    if report.exists():
        R.REPORTS.mkdir(parents=True, exist_ok=True)
        moved = _free_name(R.REPORTS, f"{slug}.attempt-parked")
        os.replace(report, moved)
    (R.PARKED / f"{slug}.why.txt").unlink(missing_ok=True)
    return moved


def requeue(slug: str, note: str | None = None, merge_only: bool = False) -> str:
    if merge_only:
        return requeue_merge_only(slug, note)
    slug = slug[:-3] if slug.endswith(".md") else slug
    running = running_slugs()
    if slug in running:
        raise RequeueError(f"{slug} is running ({running[slug]}); wait for it to finish")
    parked = R.PARKED / f"{slug}.md"
    if not parked.exists():
        raise RequeueError(f"no parked task {parked.relative_to(R.REPO)}")
    target = R.QUEUE / f"{slug}.md"
    if target.exists():
        raise RequeueError(f"{target.relative_to(R.REPO)} already exists")

    when = R.now().isoformat(timespec="minutes")
    text = parked.read_text(encoding="utf-8").rstrip("\n")
    text += f"\n\nRe-queued {when}: {note}\n" if note else f"\n\nRe-queued {when}\n"
    R.QUEUE.mkdir(parents=True, exist_ok=True)
    tmp = parked.with_suffix(".requeue.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)  # the runner never sees a half-written task
    parked.unlink()

    moved = _move_parked_leftovers(slug)
    _reset_state(slug)

    R.log(f"REQUEUED {slug}" + (f": {note}" if note else ""))
    why = R.why_not_runnable(R.parse_task(target), R.load_state())
    lines = [f"re-queued {target.relative_to(R.REPO)}"]
    if moved:
        lines.append(f"report kept as {moved.relative_to(R.REPO)}")
    lines.append(f"now: {why or 'runnable'}")
    return "\n".join(lines)


def branch_exists(branch: str) -> bool:
    return R.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


def find_source(slug: str) -> Path | None:
    """The parked copy if there is one (it is the live task), else the newest archived copy."""
    parked = R.PARKED / f"{slug}.md"
    if parked.exists():
        return parked
    archived = sorted(R.ARCHIVE.glob(f"*/{slug}.md"), key=lambda p: p.parent.name)
    return archived[-1] if archived else None


def merge_paragraph(branch: str, when: str, note: str | None) -> str:
    return R.C["merge_only_paragraph"].format(mark=MERGE_MARK, branch=branch, when=when,
                                              note=f": {note}" if note else "")


def merge_only_text(text: str, branch: str, when: str, note: str | None) -> str:
    """Priority: urgent in the header, the merge paragraph just before ## Gate."""
    lines = text.replace("\r\n", "\n").rstrip("\n").split("\n")
    for i, line in enumerate(lines[1:20], start=1):
        m = R.HEADER.match(line)
        if m and m.group(1).lower() == "priority":
            lines[i] = "Priority: urgent"
            break
    else:
        at = next((i + 1 for i, l in enumerate(lines) if l.startswith("# ")), 0)
        lines.insert(at, "Priority: urgent")
    lines = [l for l in lines if not l.startswith(MERGE_MARK)]  # parked by a merge-only run: one paragraph
    body = "\n".join(lines)
    para = merge_paragraph(branch, when, note)
    m = re.search(r"^##\s*Gate\s*$", body, re.M | re.I)
    if m:
        body = body[:m.start()] + para + "\n\n" + body[m.start():]
    else:
        body += "\n\n" + para
    return body + "\n"


def requeue_merge_only(slug: str, note: str | None = None) -> str:
    slug = slug[:-3] if slug.endswith(".md") else slug
    running = running_slugs()
    if slug in running:
        raise RequeueError(f"{slug} is running ({running[slug]}); wait for it to finish")
    branch = f"review/{slug}"
    if not branch_exists(branch):
        raise NoBranchError(f"branch {branch} does not exist, so there is nothing to merge; "
                            f"use a plain requeue to redo the work")
    target = R.QUEUE / f"{slug}.md"
    if target.exists():
        if any(l.startswith(MERGE_MARK) and f"`{branch}`" in l
               for l in target.read_text(encoding="utf-8").splitlines()):
            _reset_state(slug)  # idempotent: same queue file, attempts zeroed again
            why = R.why_not_runnable(R.parse_task(target), R.load_state())
            return f"already queued merge-only: {target.relative_to(R.REPO)}\nnow: {why or 'runnable'}"
        raise RequeueError(f"{target.relative_to(R.REPO)} already exists")
    source = find_source(slug)
    if source is None:
        raise RequeueError(f"no {slug}.md in {R.PARKED.relative_to(R.REPO)} "
                           f"or {R.ARCHIVE.relative_to(R.REPO)}/*/")

    when = R.now().isoformat(timespec="minutes")
    text = merge_only_text(source.read_text(encoding="utf-8"), branch, when, note)
    R.QUEUE.mkdir(parents=True, exist_ok=True)
    tmp = R.QUEUE / f"{slug}.requeue.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)  # the runner never sees a half-written task

    moved = None
    if source.parent == R.PARKED:  # an archived copy is history and stays put
        source.unlink()
        moved = _move_parked_leftovers(slug)
    _reset_state(slug)

    R.log(f"REQUEUED {slug} merge-only ({branch})" + (f": {note}" if note else ""))
    why = R.why_not_runnable(R.parse_task(target), R.load_state())
    lines = [f"re-queued {target.relative_to(R.REPO)} merge-only, from {source.relative_to(R.REPO)}"]
    if moved:
        lines.append(f"report kept as {moved.relative_to(R.REPO)}")
    lines.append(f"now: {why or 'runnable'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slug", help="file name in .review/parked/ (or .review/archive/*/ with --merge-only), "
                                 "with or without .md")
    ap.add_argument("--note", help="why it is going round again; appended to the task")
    ap.add_argument("--merge-only", action="store_true",
                    help="the work is already on branch review/<slug>: queue it urgent to merge that "
                         "branch and re-run the gate")
    a = ap.parse_args(argv)
    try:
        print(requeue(a.slug, a.note, merge_only=a.merge_only))
    except NoBranchError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except RequeueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
