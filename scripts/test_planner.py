"""planner.py against a fake vault and a scratch task list.

Never touches his real notes or tasks: VAULT, TASKS_FILE, PENDING_FILE
and the vault mirror are all redirected first.
"""
import datetime as dt
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import planner  # noqa: E402
import tasks  # noqa: E402

tmp = tempfile.mkdtemp(prefix="denisse-planner-")
tasks.TASKS_FILE = os.path.join(tmp, "tasks.json")
tasks._mirror_to_vault = lambda: None
planner.PENDING_FILE = os.path.join(tmp, "pending_plan.json")
vault = os.path.join(tmp, "vault")


def note(rel, text):
    p = os.path.join(vault, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(text)


note(r"SEM 1 Modules\5CCYB010 Electromagnetism\Coursework.md",
     '---\ndescription: "5CCYB010 coursework, 30%, due 24 Nov 2026 — brief not yet read"\n---\n')
note(r"SEM 1 Modules\5CCYB020 Signals and Systems\Coursework\Coursework.md",
     '---\ndescription: "Signals coursework"\n---\n> Due **Tuesday 24 November 2026, 16:00** on KEATS.\n')
note(r"SEM 1 Modules\5CCYB020 Signals and Systems\Coursework\Worked Solutions.md",
     "due 1 Jan 2026 -- not a deadline file\n")
note(r"SEM 1 Modules\5CCYB099 Old Module\Coursework.md",
     '---\ndescription: "due 1 Mar 2026"\n---\n')
planner.VAULT = vault
# coursework_deadlines' default argument was bound at import -- pass it.
_orig = planner.coursework_deadlines
planner.coursework_deadlines = lambda vault=vault: _orig(vault)

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


now = dt.datetime(2026, 10, 29, 11, 0)
dl = planner.coursework_deadlines()
check("reads description and body deadlines", len(dl) == 3, [d["module"] for d in dl])
check("module name from a Coursework/ subfolder",
      any(d["module"] == "Signals and Systems" for d in dl))
check("brief-unread flag read from his note",
      [d["brief_unread"] for d in dl if d["module"] == "Electromagnetism"] == [True])

tasks.add_task("msoc: chase Samantha", due="27 Oct 2026 12:00")
b = planner.briefing(now)
check("briefing names the overdue task", "chase Samantha" in b, b[:120])
check("briefing names the coursework", "Electromagnetism" in b and "24 Nov" in b)
check("briefing carries no weather or news",
      not any(w in b.lower() for w in ("°", "weather", "headline", "news")))
check("past deadlines are not upcoming", "Old Module" not in b)

text, steps = planner.study_plan(now)
check("plan is dated from the deadline", "Tue 17 Nov" in text and "Mon 23 Nov" in text, text[:90])
check("read-the-brief only where unread",
      sum("brief" in s["text"] for s in steps) == 1, [s["text"] for s in steps])
check("plan says what it doesn't know", "timetable" in text)

q = planner.offer(steps, now)
check("offer states how many", str(len(steps)) in q, q)
check("yes much later is not this plan", planner.pending(now + dt.timedelta(hours=2)) == [])
before = len(tasks.list_tasks())
added = planner.accept(now + dt.timedelta(minutes=1))
after = tasks.list_tasks()
check("accept adds exactly the held steps", len(after) - before == len(steps), added[:80])
check("accept reads back what it added", "Electromagnetism" in added)
check("pending cleared after accepting", planner.pending(now) == [])
_, again = planner.study_plan(now)
check("re-planning skips steps already on the list", again == [], [s["text"] for s in again])

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
