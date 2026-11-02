# orchestra — the task runner

How the queue works is in `.review/README.md`; limits are in `config.json`,
each with its reason. This file covers the commands.

| Command | What it does |
|---|---|
| `python orchestra\runner.py [--dry-run]` | One tick: run the top runnable task (Task Scheduler runs this every 2 min) |
| `python orchestra\new_task.py "title"` | New task file in `.review/queue/` from the template |
| `python orchestra\status.py [--json]` | One screen: running tasks per lane slot, the queue with why each task isn't runnable, parked tasks with their reason, today's results and spend, the last 5 log lines. Reads only |
| `python orchestra\requeue.py <slug> [--note "why"]` | Put a parked task back in the queue |
| `python orchestra\state_now.py` | Regenerate `.review/STATE_NOW.md` (the runner does this after every tick) |

Use the venv python (`.venv\Scripts\python.exe`) for all of them.

## Re-queuing a parked task

Don't edit `state.json` by hand. `requeue.py <slug>`:

- moves `.review/parked/<slug>.md` back to `.review/queue/` and appends
  `Re-queued <when>: <note>`, so the content (and hash) is new and the runner
  treats it as a fresh task;
- moves the parked report to `.review/reports/<slug>.attempt-parked.md`
  (`-2`, `-3`… if an earlier one is there) and deletes `<slug>.why.txt`;
- resets the task's `attempts` and `last_end` through the runner's own
  `load_state`/`save_state`, which merges with a lane runner saving at the
  same time;
- refuses if a runner lock (`.review/runner*.lock`) names the task as running,
  or if the queue already has a file of that name.

It prints whether the task is now runnable. It won't be if the reason it
parked still holds in its header, e.g. a `Written:` date past `stale_days`:
re-date the task in the queue.

Tests: `python -m unittest orchestra.test_tools orchestra.test_runner`.
