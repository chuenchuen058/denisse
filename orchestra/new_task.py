"""Create a task file from .review/TEMPLATE.md.

    python orchestra\\new_task.py "Fix tray restart" --priority urgent --weight heavy
"""
import argparse
import datetime as dt
import re
from pathlib import Path

REVIEW = Path(__file__).resolve().parent.parent / ".review"

ap = argparse.ArgumentParser()
ap.add_argument("title")
ap.add_argument("--priority", choices=["urgent", "normal"], default="normal")
ap.add_argument("--weight", choices=["light", "heavy"], default="light")
ap.add_argument("--kind", choices=["work", "maintenance"], default="work")
ap.add_argument("--lane", default=None,
                help="Default: the best-scoring working lane for the work (orchestra/exams), else claude")
ap.add_argument("--work", default=None,
                help="Kind of work for the lane choice: code-edit, debug, writing, study, extract "
                     "(or a hub kind like code_change). Default: guessed from the title")
ap.add_argument("--auto", action="store_true",
                help="Queue: auto straight away (default is draft: fill in the body and gate, then set auto)")
a = ap.parse_args()
if a.lane is None:
    import sys
    sys.path.insert(0, str(REVIEW.parent))
    from orchestra.exams.lane_for import lane_for
    a.lane = lane_for(a.work, a.title)
    print(f"lane: {a.lane} (from exam scores; --lane to choose)")


def norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def first_title(p: Path) -> str:
    try:
        line = p.read_text(encoding="utf-8").lstrip().splitlines()[0]
    except (OSError, IndexError):
        return ""
    return line[2:] if line.startswith("# ") else ""


# A same-title task is only a hint: follow-ups reuse titles legitimately (Thomas's
# title-only check blocked 4 new tasks). The runner refuses exact repeats by content hash.
cutoff = (dt.date.today() - dt.timedelta(days=14)).isoformat()
places = [REVIEW / "queue", REVIEW / "parked"]
places += [d for d in (REVIEW / "archive").glob("*") if d.is_dir() and d.name >= cutoff]
for d in places:
    for p in d.glob("*.md") if d.exists() else []:
        if not p.name.endswith(".report.md") and norm(first_title(p)) == norm(a.title):
            print(f"note: same title as {p}; fine if this is a follow-up")

stamp = dt.datetime.now().strftime("%Y-%m-%dT%H%M")
slug = re.sub(r"[^a-z0-9]+", "-", a.title.lower()).strip("-")[:50]
text = (REVIEW / "TEMPLATE.md").read_text(encoding="utf-8")
text = text.replace("<short title, what will be true when done>", a.title)
for key, val in (("Queue", "auto" if a.auto else "draft"), ("Priority", a.priority),
                 ("Weight", a.weight), ("Kind", a.kind), ("Lane", a.lane),
                 ("Written", dt.datetime.now().strftime("%Y-%m-%dT%H:%M"))):
    text = re.sub(rf"^{key}:.*$", f"{key}: {val}", text, count=1, flags=re.M)
path = REVIEW / "queue" / f"{stamp}-{slug}.md"
n = 2
while path.exists():  # never overwrite a task someone else wrote
    path = REVIEW / "queue" / f"{stamp}-{slug}-{n}.md"
    n += 1
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(text, encoding="utf-8")
print(path)
