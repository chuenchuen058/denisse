# Model, tools and standing instructions

Acceptance criteria for the first autonomous workflows:

- The local model chooses a bounded sequence of observations, then a next-step
  suggestion; a plan is not counted as an executed action.
- Task review reads the actual task store without ticking tasks or importing
  vault checkboxes. Project review runs fixed read-only Git commands in the
  configured project. Neither may call shell, task mutation or arbitrary files.
- Downloads uses the existing deterministic categorisation rules, with one
  watcher, collision handling and quiet-file checks. It works with Ollama down.
- Instructions survive restart. Each has its own enabled switch and interval;
  pause stops future actions. Invalid settings stop jobs rather than widening
  permissions. An interrupted run cannot immediately repeat after restart.
- Every completed review has the required successful observations. Invented
  tools, unsupported task IDs, tool errors and exhausted budgets do not count
  as success. Pausing during planning prevents the next tool call.
- Concurrent workers cannot run the same instruction twice. Unchanged reviews
  stay quiet; reports remain local and do not enter chat history or training.

These criteria are exercised by `scripts/test_standing_instructions.py` with
synthetic tasks, files and model decisions. Real-model quality is evaluated
separately; deterministic checks do not establish the quality of suggestions.

## The three parts

1. **Model:** `agent_loop.py` uses the same local Ollama model and 8192-token
   context as chat. It selects an observation, sees the result and proposes a
   next step. Successful observations disappear from its next choices. Four
   decisions, a checked run deadline and finite request/output limits stop
   repeated calls. This adds an agent workflow, not new weights or sentience.
2. **Tools:** `agent_tools.py` binds task reads, project Git reads and the
   existing Downloads organiser. The model supplies no paths or shell commands.
   Task review cannot complete tasks; project review cannot modify or commit.
   Reports distinguish observed facts from model suggestions.
3. **Instructions:** `standing_instructions.py` loads private persistent goals,
   enabled flags, intervals and the project path. Defaults: tasks every six
   hours, project every hour, Downloads every 15 seconds. Downloads rules are
   deterministic and do not depend on Ollama or the free-text goal.

## Setup and controls

```powershell
.venv\Scripts\python.exe scripts/standing.py --init --enable
.venv\Scripts\python.exe scripts/standing.py
.venv\Scripts\python.exe tray.py
```

Initialisation never overwrites existing instructions. For a fresh setup all
three jobs are configured. Existing installations with no instruction file
retain the previous Downloads-only behaviour until reviews are enabled.

Edit `memory/standing_instructions.json` to change supported goals, intervals
or `project_dir`; use an absolute repository-root path. The project defaults
to the Denisse repository. Changes are picked up without restarting the tray.
This first version supports these three job types; arbitrary tools, code edits,
email sending or new job types require an implementation change.

The tray's **Independent work** switch pauses/resumes all three jobs.
**Auto-organize Downloads** controls that job while independent work is enabled.
**What Denisse did** displays the latest activity beside the orb. Changes
produce short notifications; unchanged observations stay quiet. Existing
deadline reminders and temporary-file maintenance remain separate features.
Model reviews wait while chat is busy or Denisse is manually offline.

```powershell
# Pause/resume everything, or just one job:
.venv\Scripts\python.exe scripts/standing.py --pause
.venv\Scripts\python.exe scripts/standing.py --enable
.venv\Scripts\python.exe scripts/standing.py --pause --job downloads
.venv\Scripts\python.exe scripts/standing.py --enable --job task_review
# Inspect a due enabled job once; respects cooldowns and process locks:
.venv\Scripts\python.exe scripts/standing.py --run project_review
```

The tray must be running; this is not a new Windows/Codex scheduled task.
Sleeping/off machines do not run jobs or replay every missed interval on wake.
A pause prevents the next tool call; it does not undo an earlier file move or
instantly cancel an in-flight Ollama HTTP request. The two worker threads keep
Downloads moving while model reviews wait. OS locks prevent duplicate jobs
across processes. Jobs claim their interval before executing, including on
failure; restart does not immediately repeat an interrupted action.

## Records and verification

`memory/agent_state.json` retains last-start times and outcomes.
`logs/agent_runs/*-latest.json` retains the latest review/changed Downloads
record, including decisions, actual evidence and errors. Existing
`memory/organize_log.json` records individual moves. All are private/ignored,
and none enters personal chat replay or the learning dataset. Corrupt state
stops execution; inspect it before resetting rather than silently replaying.

Task review observes at most 20 open tasks in deadline order. Project review
checks local Git state only; it does not establish that code works or is pushed.
The latest records are bounded snapshots, not a permanent complete event log.
Free-text goals guide reviews inside the fixed capabilities; changing a goal
does not grant extra permissions or redesign the Downloads categories.

```powershell
.venv\Scripts\python.exe scripts/test_standing_instructions.py
.venv\Scripts\python.exe scripts/eval_standing_instructions.py --rounds 3 --out logs/standing-model-eval.json
```

The first real-model implementation repeated already-completed observations
and completed 0/12 synthetic trials. Removing completed observations from the
next decision choices produced 12/12 completions over three rounds. Later tests
also caught invented task IDs during project review; the decoder now offers
only observed IDs, with an independent execution-time check. These tests
measure scoped execution and completion, not general reasoning quality;
early suggestions were often shallow, so practical usefulness remains a
separate criterion. Downloads effects are tested with real temporary files.
