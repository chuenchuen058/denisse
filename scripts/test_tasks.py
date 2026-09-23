"""Regression test for tasks.py -- deadline parsing and task matching.

Uses a temp store so it never touches memory/tasks.json.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tasks

fails = []
# Wednesday 2026-09-23 21:00, so "friday" is 2 days ahead.
NOW = datetime(2026, 9, 23, 21, 0)

print("== deadline parsing ==")
cases = [
    ("before friday", "2026-09-25 23:59"),
    ("by friday 5pm", "2026-09-25 17:00"),
    ("friday", "2026-09-25 23:59"),
    ("next monday 9am", "2026-09-28 09:00"),
    ("tomorrow 5pm", "2026-09-24 17:00"),
    ("in 2 hours", "2026-09-23 23:00"),
    ("wednesday", "2026-09-30 23:59"),   # today is Wed -> next week, not today
    ("", None),
    ("sometime whenever", None),
]
for text, want in cases:
    got = tasks.parse_due(text, now=NOW)
    got_s = got.strftime("%Y-%m-%d %H:%M") if got else None
    ok = got_s == want
    print(f"[{'OK ' if ok else 'FAIL'}] {text!r:22} -> {got_s}  (want {want})")
    if not ok:
        fails.append(text)


print("\n== task store and matching ==")
import tempfile
tasks.TASKS_FILE = str(Path(tempfile.mkdtemp()) / "tasks.json")

tasks.add_task("email Sunway about the firm visit", "before friday")
tasks.add_task("submit the EE problem set", "tomorrow 5pm")
tasks.add_task("buy a new soldering tip")

open_now = tasks.list_tasks()
checks = [
    ("three open tasks", len(open_now) == 3),
    ("soonest deadline first", open_now[0]["text"].startswith("submit")),
    ("undated sorts last", open_now[-1]["text"].startswith("buy")),
    ("raw phrase kept", open_now[0]["due_text"] == "tomorrow 5pm"),
]

# Fuzzy match on words, not the id.
done = tasks.complete_task("the sunway email")
checks.append(("fuzzy match completes the right task",
               done is not None and "Sunway" in done["text"]))
checks.append(("done task hidden", len(tasks.list_tasks()) == 2))
checks.append(("done task kept", len(tasks.list_tasks(include_done=True)) == 3))
checks.append(("completing it again finds nothing",
               tasks.complete_task("the sunway email") is None))

# Ambiguity must refuse rather than guess.
tasks.add_task("call mum about the flight")
tasks.add_task("book the flight")
checks.append(("ambiguous match refuses", tasks.complete_task("flight") is None))
checks.append(("specific match still works",
               tasks.complete_task("book flight") is not None))

for name, ok in checks:
    print(f"[{'OK ' if ok else 'FAIL'}] {name}")
    if not ok:
        fails.append(name)

print("\n" + ("ALL PASS" if not fails else "FAILED: " + ", ".join(map(str, fails))))
sys.exit(1 if fails else 0)
