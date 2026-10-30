"""study_brief.py: the whole job against a fake vault and a scratch task list.

Never touches his real notes, tasks or job state: VAULT, DRAFTS, JOBS_DIR,
TASKS_FILE, PENDING_FILE and the vault mirror are redirected first.
"""
import datetime as dt
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import brain  # noqa: E402
import planner  # noqa: E402
import study_brief as sb  # noqa: E402
import tasks  # noqa: E402

tmp = tempfile.mkdtemp(prefix="denisse-brief-")
tasks.TASKS_FILE = os.path.join(tmp, "tasks.json")
tasks._mirror_to_vault = lambda: None
planner.PENDING_FILE = os.path.join(tmp, "pending_plan.json")
vault = os.path.join(tmp, "vault")
sb.VAULT = planner.VAULT = vault
sb.DRAFTS = os.path.join(vault, "Denisse", "Drafts")
sb.JOBS_DIR = os.path.join(tmp, "jobs")
_orig = planner.coursework_deadlines
planner.coursework_deadlines = lambda vault=vault: _orig(vault)


def note(rel, text):
    p = os.path.join(vault, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(text)


SS = r"SEM 1 Modules\5CCYB020 Signals and Systems"
note(SS + r"\Coursework\Coursework.md", """---
description: "5CCYB020 summative coursework (30%)"
---
> [!important] Due **Tuesday 24 November 2026, 16:00** on KEATS.

| Q | Topic | Marks | Revise from |
|---|---|---|---|
| **1** | Signals & the Dirac delta — sketch | 14 | Wk 1 lecture; Wk 3 tutorial |
| **2** | Fourier transform — Re, Im, \\|Ψ\\| on 3 graphs | 16 | Wk 4 lecture (FT) |
| **3** | Laplace properties — table | 10 | Laplace lecture (later week) |

## Progress

- [x] Q1 sketches
- [ ] Q2 three plots
- [ ] Q3 (i)-(iv)
- [ ] Assemble single report, submit
""")
for n, status in ((1, ""), (3, ""), (4, "scaffolded")):
    note(SS + rf"\Week {n}\Signals and Systems Week {n}.md",
         f"---\nweek: {n}\ntopic: Topic {n}\n" + (f"status: {status}\n" if status else "") + "---\n")
EM = r"SEM 1 Modules\5CCYB010 Electromagnetism"
note(EM + r"\Coursework.md", """---
description: "5CCYB010 coursework, 30%, due 24 Nov 2026 — brief not yet read"
---
## Which week notes feed it

- [[Electromagnetism Week 1|Week 1]]
- [[Electromagnetism Week 2|Week 2]] **(scaffolded)**

## Plan

- [ ] Read the brief and replace the guesses above
- [ ] Draft
- [ ] Submit on KEATS
""")
note(EM + r"\Week 1\Electromagnetism Week 1.md", "---\nweek: 1\ntopic: Vectors\n---\n")
note(EM + r"\Week 2\Electromagnetism Week 2.md", "---\nweek: 2\ntopic: Gauss\nstatus: files-only\n---\n")

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


now = dt.datetime(2026, 10, 30, 19, 0)

# -- which module, and which requests are a study brief --------------------
for text, want in (("prep me for signals and systems", "Signals and Systems"),
                   ("brief me on electromagnetism", "Electromagnetism"),
                   ("help me revise cab", "Computational Applied Biomathematics"),
                   ("comp methods please", "Computational Methods"),
                   ("tell them about the system", None), ("prep me for them", None)):
    check(f"module {text!r}", sb.match_module(text) == want, sb.match_module(text))
for text in ("prep me for signals and systems", "study brief for electromagnetism",
             "brief me on em", "help me revise cab", "revise for signals",
             "get me ready for computational methods"):
    check(f"routes to the brief: {text!r}", bool(brain._STUDY_BRIEF.search(text)))
for text in ("prep me on what to say for the pitch", "help me revise this paragraph",
             "brief me", "tell me the briefing", "plan my study modules",
             "prep me for the JPMorgan interview"):
    check(f"not a brief: {text!r}", not brain._STUDY_BRIEF.search(text))
check("the briefing still goes to the briefing", bool(brain._BRIEFING.search("brief me")))

# -- reading his notes -----------------------------------------------------
cw = sb.read_coursework(os.path.join(vault, SS, "Coursework", "Coursework.md"))
q2 = next(i for i in cw["items"] if i["text"].startswith("Q2"))
check("escaped pipe stays inside the cell", q2["marks"] == 16 and q2["weeks"] == [4], q2)
check("ticked items read as done", [i["done"] for i in cw["items"]] == [True, False, False, False])
check("a question with no taught week has no weeks",
      next(i for i in cw["items"] if i["text"].startswith("Q3"))["weeks"] == [])
weeks = sb.read_weeks(os.path.join(vault, SS))
check("week status read", [(w["week"], w["written"]) for w in weeks] == [(1, True), (3, True), (4, False)])

# -- the schedule stays inside the window ----------------------------------
today, due = dt.date(2026, 10, 30), dt.date(2026, 11, 24)
plan = sb.schedule([{"text": "a", "weight": 14}, {"text": "b", "weight": 16}], today, due)
check("plan dates between today and the deadline",
      all(today <= d <= due for d, _ in plan), plan)
check("submit is the day before", plan[-1] == (dt.date(2026, 11, 23), "Assemble, check and submit on KEATS"))
late = sb.schedule([{"text": "a"}], dt.date(2026, 11, 23), due)
check("a day before the deadline still fits", all(d <= due for d, _ in late), late)

# -- the whole job ---------------------------------------------------------
tasks.add_task("Write up Electromagnetism Week 2")
reply = sb.run("prep me for signals and systems", now)
path = os.path.join(sb.DRAFTS, "2026-10-30 Signals and Systems study brief.md")
check("brief written into Drafts", os.path.exists(path), reply[:120])
text = open(path, encoding="utf-8").read()
check("brief has the due date and the unticked items",
      "Tue 24 Nov 2026" in text and "Q2 three plots" in text and "Q3 (i)-(iv)" in text)
check("a ticked item is not in what's left", "| Q1 sketches |" not in text)
check("week 4 written up before Q2",
      text.index("Write up Week 4") < text.index("— Q2 three plots"))
check("reply says where and offers the tasks",
      "Denisse\\Drafts" in reply and "to your tasks?" in reply, reply)
check("the brief passes its own check", sb.verify(path, json.load(open(sb._job_file("Signals and Systems"))),
                                                  today) == [])
check("job state saved as done", json.load(open(sb._job_file("Signals and Systems")))["status"] == "done")

answer = planner.accept(now)
check("'yes' adds the steps", answer.startswith("Added ") and "Signals and Systems" in answer, answer[:80])

# a write-up already on his list is not planned again
reply = sb.run("study brief for electromagnetism", now)
text = open(os.path.join(sb.DRAFTS, "2026-10-30 Electromagnetism study brief.md"), encoding="utf-8").read()
check("existing write-up task not duplicated", "Write up Week 2" not in text and "already on your task list" in text)
check("no question table: the Plan list is the work",
      "- [ ] Draft" in text and "Read the Electromagnetism coursework brief" in text)

# -- his edits survive a rerun ---------------------------------------------
with open(path, "a", encoding="utf-8") as f:
    f.write("\nMy own note.\n")
sb.run("prep me for signals", now)
check("an edited brief is not overwritten", "My own note." in open(path, encoding="utf-8").read())
check("hers goes beside it", os.path.exists(path[:-3] + " (2).md"))

# -- a broken link is caught -----------------------------------------------
bad = os.path.join(tmp, "bad.md")
open(bad, "w", encoding="utf-8").write("Tue 24 Nov 2026 [[No Such Note]] Q2 three plots Q3 (i)-(iv)\n")
job = json.load(open(sb._job_file("Signals and Systems")))
check("verify catches a broken link", any("No Such Note" in p for p in sb.verify(bad, job, today)))

# -- a step that fails is reported, not faked, and the next run resumes ----
os.remove(sb._job_file("Signals and Systems"))
real_drafts = sb.DRAFTS
sb.DRAFTS = os.path.join(tmp, "a-file")
open(sb.DRAFTS, "w").write("not a folder")
planner.clear_pending()
reply = sb.run("prep me for signals", now)
check("failed write is said plainly", reply.startswith("I couldn't finish") and "write" in reply, reply)
check("nothing offered after a failure", not planner.pending(now))
job = json.load(open(sb._job_file("Signals and Systems")))
check("failure recorded with the step", job["status"] == "failed" and "write" in job["errors"], job["errors"])

# interrupted mid-run: finished steps are reused, the rest done
sb.DRAFTS = real_drafts
job.update(status="running", errors={})
job["done"].pop("write", None)
job["done"]["deadline"]["weight"] = "RESUMED"  # a marker only a reused step would carry
json.dump(job, open(sb._job_file("Signals and Systems"), "w"))
reply = sb.run("prep me for signals", now + dt.timedelta(minutes=5))
check("an interrupted run picks up where it stopped",
      "Picked up where the last run stopped" in reply, reply[-80:])
check("...reusing the finished steps", "RESUMED" in open(path[:-3] + " (2).md", encoding="utf-8").read())

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
