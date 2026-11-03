# Orchestra: treat an agent's usage limit as "come back later", not a failed attempt
Queue: auto
Priority: urgent
Weight: light
Kind: work
Lane: claude
Written: 2026-11-02T19:56

On 2 Nov the Codex lane printed `ERROR: You've hit your usage limit ... try again at 10:55 PM.` and the runner logged NO_REPORT, used up the task's retry and parked it. Claude CLI has similar messages ("usage limit reached", "rate limit", "resets at ..."), Gemini prints IneligibleTierError / quota errors.

In `orchestra/runner.py`:
1. After a lane run with no report, scan the tail of `<slug>.lane.log` for known limit messages per lane (keep the patterns in one table, with the real Codex line above as a test fixture). If found: log `LIMITED <slug> lane=<lane> until <time>`, do NOT count the attempt, and record `limited_until` for that lane in state.json (parse "try again at 10:55 PM" / "resets at 3am" style times into local datetimes; if unparseable, now + 60 min).
2. `why_not_runnable` returns "lane <x> limited until HH:MM" for tasks on a limited lane.
3. Optional fallback: config `lane_fallback` (default `{"codex": "claude"}`) — while a lane is limited, a task whose header has no `Lane-Fixed: yes` runs on the fallback lane instead (log it, and note it in the vault note via the existing aftercare path). Respect the claude lane slots.
4. `STATE NOW` (orchestra/state_now.py) shows limited lanes and until when.
Tests in `orchestra/test_runner.py` with the fake lane printing the Codex limit message.

## Gate
```
.venv\Scripts\python.exe -m unittest orchestra.test_runner -v
```
