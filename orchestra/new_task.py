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
a = ap.parse_args()

stamp = dt.datetime.now().strftime("%Y-%m-%dT%H%M")
slug = re.sub(r"[^a-z0-9]+", "-", a.title.lower()).strip("-")[:50]
text = (REVIEW / "TEMPLATE.md").read_text(encoding="utf-8")
text = text.replace("<short title, what will be true when done>", a.title)
for key, val in (("Queue", "auto" if a.auto else "draft"), ("Priority", a.priority),
                 ("Weight", a.weight), ("Kind", a.kind), ("Lane", a.lane),
                 ("Written", dt.datetime.now().strftime("%Y-%m-%dT%H:%M"))):
    text = re.sub(rf"^{key}:.*$", f"{key}: {val}", text, count=1, flags=re.M)
path = REVIEW / "queue" / f"{stamp}-{slug}.md"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(text, encoding="utf-8")
print(path)
