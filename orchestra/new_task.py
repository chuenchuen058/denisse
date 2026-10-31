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
ap.add_argument("--lane", default="claude")
ap.add_argument("--auto", action="store_true",
                help="Queue: auto straight away (default is draft: fill in the body and gate, then set auto)")
ap.add_argument("--allow-duplicate", action="store_true",
                help="write it even though a task with the same title is open, parked or archived in 14 days")
a = ap.parse_args()


def norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def first_title(p: Path) -> str:
    try:
        line = p.read_text(encoding="utf-8").lstrip().splitlines()[0]
    except (OSError, IndexError):
        return ""
    return line[2:] if line.startswith("# ") else ""


# Refuse duplicates instead of piling them up (Thomas: automated writers re-filed tasks
# about things already fixed, hundreds of times). Same title = duplicate.
cutoff = (dt.date.today() - dt.timedelta(days=14)).isoformat()
places = [REVIEW / "queue", REVIEW / "parked"]
places += [d for d in (REVIEW / "archive").glob("*") if d.is_dir() and d.name >= cutoff]
for d in places:
    for p in d.glob("*.md") if d.exists() else []:
        if p.name.endswith(".report.md"):
            continue
        if norm(first_title(p)) == norm(a.title) and not a.allow_duplicate:
            raise SystemExit(f"refused: same title as {p} (pass --allow-duplicate if it isn't one)")

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
