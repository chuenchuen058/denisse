"""Create a task file from .review/TEMPLATE.md.

    python orchestra\\new_task.py "Fix tray restart" --priority urgent --weight heavy
    python orchestra\\new_task.py "Summarise week 5" --auto --body-file brief.md \\
        --gate ".venv\\Scripts\\python.exe scripts\\run_checks.py --fast" --from agent-hub

Or from Python, for any agent that hands work to Orchestra (Chuen, 9 Nov 2026: every
agent's work goes through the queue, so it shows in runner.log, STATE_NOW and the hub):

    from orchestra.new_task import create
    path = create("Summarise week 5", body="...", gate=["..."], auto=True, source="agent-hub")
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

# ORCHESTRA_REPO: the checkout whose queue it is, as for the runner (the hub's tests point it at a temp repo).
REVIEW = Path(os.environ.get("ORCHESTRA_REPO") or Path(__file__).resolve().parent.parent) / ".review"
PRIORITIES = ("urgent", "normal")
WEIGHTS = ("light", "heavy")
def _bugfix_kinds() -> tuple[str, ...]:
    """Kinds that mean a bug or error fix (config bugfix_kinds; the runner routes them to bugfix_lane)."""
    here = Path(__file__).resolve().parent
    for name in ("config.json", "config_defaults.json"):
        try:
            kinds = json.loads((here / name).read_text(encoding="utf-8")).get("bugfix_kinds")
        except (OSError, ValueError):
            continue
        if isinstance(kinds, list) and all(isinstance(k, str) for k in kinds):
            return tuple(k.lower() for k in kinds)
    return ()


KINDS = ("work", "maintenance", *_bugfix_kinds())
TITLE_PLACEHOLDER = "<short title, what will be true when done>"
SAME_TITLE_DAYS = 14


def norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def first_title(p: Path) -> str:
    try:
        line = p.read_text(encoding="utf-8").lstrip().splitlines()[0]
    except (OSError, IndexError):
        return ""
    return line[2:] if line.startswith("# ") else ""


def same_title(title: str, review: Path | None = None) -> list[Path]:
    """Tasks queued, parked or archived in the last SAME_TITLE_DAYS with this title. Only a
    hint: follow-ups reuse titles legitimately (Thomas's title-only check blocked 4 new
    tasks). The runner refuses exact repeats by content hash."""
    review = Path(review) if review else REVIEW
    cutoff = (dt.date.today() - dt.timedelta(days=SAME_TITLE_DAYS)).isoformat()
    places = [review / "queue", review / "parked"]
    places += [d for d in (review / "archive").glob("*") if d.is_dir() and d.name >= cutoff]
    out = []
    for d in places:
        for p in d.glob("*.md") if d.exists() else []:
            if not p.name.endswith(".report.md") and norm(first_title(p)) == norm(title):
                out.append(p)
    return out


def default_lane(work: str | None, title: str) -> str:
    """The best-scoring working lane for the work (orchestra/exams), else claude."""
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from orchestra.exams.lane_for import lane_for
    return lane_for(work, title)


def render(title: str, *, body: str | None = None, gate: list[str] | None = None, priority: str = "normal",
           weight: str = "light", kind: str = "work", lane: str = "claude", auto: bool = False,
           source: str | None = None, review: Path | None = None, when: dt.datetime | None = None) -> str:
    """The task file's text: the template with the title, headers, and (if given) the body and
    the gate commands filled in."""
    for value, allowed, what in ((priority, PRIORITIES, "priority"), (weight, WEIGHTS, "weight"),
                                 (kind, KINDS, "kind")):
        if value not in allowed:
            raise ValueError(f"{what} must be one of {', '.join(allowed)}, not {value!r}")
    title = " ".join(str(title).split())
    if not title:
        raise ValueError("a task needs a title")
    when = when or dt.datetime.now()
    review = Path(review) if review else REVIEW
    text = (review / "TEMPLATE.md").read_text(encoding="utf-8")
    text = text.replace(TITLE_PLACEHOLDER, title)
    for key, val in (("Queue", "auto" if auto else "draft"), ("Priority", priority),
                     ("Weight", weight), ("Kind", kind), ("Lane", lane),
                     ("Written", when.strftime("%Y-%m-%dT%H:%M"))):
        text = re.sub(rf"^{key}:.*$", lambda _m, k=key, v=val: f"{k}: {v}", text, count=1, flags=re.M)
    if source:
        text = re.sub(r"^(Written:.*)$", lambda m: f"{m.group(1)}\nFrom: {' '.join(source.split())}", text,
                      count=1, flags=re.M)
    head, sep, gate_part = text.partition("\n## Gate")
    if body is not None:
        # The template's placeholder paragraph (<...>) after the headers is the body.
        head = re.sub(r"\n\n<[^>]*>\s*$", lambda _m: "\n\n" + body.strip() + "\n", head, count=1)
    if gate is not None and sep:
        gate_part = "\n```\n" + "\n".join(gate) + "\n```\n"
    return head + sep + gate_part


def create(title: str, *, body: str | None = None, gate: list[str] | None = None, priority: str = "normal",
           weight: str = "light", kind: str = "work", lane: str | None = None, work: str | None = None,
           auto: bool = False, source: str | None = None, review: Path | None = None, say=None) -> Path:
    """Write a new task into <review>/queue and return its path. Never overwrites a task
    someone else wrote. `lane` None: the best-scoring lane for `work` (exam scores).
    `source` names the agent that handed the work over (a From: header)."""
    say = say or (lambda _s: None)
    review = Path(review) if review else REVIEW
    if lane is None:
        lane = default_lane(work, title)
        say(f"lane: {lane} (from exam scores; --lane to choose)")
    for p in same_title(title, review):
        say(f"note: same title as {p}; fine if this is a follow-up")
    when = dt.datetime.now()
    text = render(title, body=body, gate=gate, priority=priority, weight=weight, kind=kind, lane=lane,
                  auto=auto, source=source, review=review, when=when)
    stamp = when.strftime("%Y-%m-%dT%H%M")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
    queue = review / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    path = queue / f"{stamp}-{slug}.md"
    n = 2
    while True:
        try:
            with path.open("x", encoding="utf-8") as f:  # never overwrite a task someone else wrote
                f.write(text)
            return path
        except FileExistsError:
            path = queue / f"{stamp}-{slug}-{n}.md"
            n += 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("title")
    ap.add_argument("--priority", choices=PRIORITIES, default="normal")
    ap.add_argument("--weight", choices=WEIGHTS, default="light")
    ap.add_argument("--kind", choices=KINDS, default="work")
    ap.add_argument("--lane", default=None,
                    help="Default: the best-scoring working lane for the work (orchestra/exams), else claude")
    ap.add_argument("--work", default=None,
                    help="Kind of work for the lane choice: code-edit, debug, writing, study, extract "
                         "(or a hub kind like code_change). Default: guessed from the title")
    ap.add_argument("--auto", action="store_true",
                    help="Queue: auto straight away (default is draft: fill in the body and gate, then set auto)")
    ap.add_argument("--body-file", default=None, help="the task's body (UTF-8 text), instead of the template's")
    ap.add_argument("--gate", action="append", default=None, help="a gate command (repeat for several)")
    ap.add_argument("--from", dest="source", default=None, help="the agent handing the work over (From: header)")
    a = ap.parse_args(argv)
    body = Path(a.body_file).read_text(encoding="utf-8") if a.body_file else None
    try:
        path = create(a.title, body=body, gate=a.gate, priority=a.priority, weight=a.weight, kind=a.kind,
                      lane=a.lane, work=a.work, auto=a.auto, source=a.source, say=print)
    except ValueError as e:
        print(f"new_task: {e}", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
