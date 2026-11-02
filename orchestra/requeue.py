"""Put a parked task back in the queue, so nobody edits state.json by hand.

    .venv\\Scripts\\python.exe orchestra\\requeue.py <slug> [--note "why"]

Moves .review/parked/<slug>.md back to .review/queue/ with a `Re-queued <when>: <note>`
line appended (new content, so the runner treats it as a fresh task), moves its report
to .review/reports/<slug>.attempt-parked.md, deletes <slug>.why.txt, and resets the
task's attempts and last_end in state.json through the runner's own load/save (which
merge with a lane runner saving at the same time). Refuses while the task is running.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner as R  # noqa: E402


class RequeueError(Exception):
    pass


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


def requeue(slug: str, note: str | None = None) -> str:
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

    report = R.PARKED / f"{slug}.report.md"
    moved = None
    if report.exists():
        R.REPORTS.mkdir(parents=True, exist_ok=True)
        moved = _free_name(R.REPORTS, f"{slug}.attempt-parked")
        os.replace(report, moved)
    (R.PARKED / f"{slug}.why.txt").unlink(missing_ok=True)

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

    R.log(f"REQUEUED {slug}" + (f": {note}" if note else ""))
    why = R.why_not_runnable(R.parse_task(target), R.load_state())
    lines = [f"re-queued {target.relative_to(R.REPO)}"]
    if moved:
        lines.append(f"report kept as {moved.relative_to(R.REPO)}")
    lines.append(f"now: {why or 'runnable'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slug", help="file name in .review/parked/, with or without .md")
    ap.add_argument("--note", help="why it is going round again; appended to the task")
    a = ap.parse_args(argv)
    try:
        print(requeue(a.slug, a.note))
    except RequeueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
