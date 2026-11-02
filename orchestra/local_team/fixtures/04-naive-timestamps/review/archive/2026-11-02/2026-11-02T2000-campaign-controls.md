# Campaign controls: a hard deadline and pause/resume/stop that the runner itself enforces
Queue: auto
Priority: urgent
Weight: heavy
Kind: work
Lane: claude
Written: 2026-11-02T20:00

From the owner's master prompt (`<private path>`, sections 5 and 11; read them). Today the "Max-plan sprint" (claude lane_slots 3, notional cap $400) ends Wed 4 Nov 18:00 Europe/London only because a scheduled Claude chat will edit config.json then. Put that authority in deterministic code.

1. `.review/campaign.json` (private runtime area; schema version, validated on load; a worker can't change it — guard.py must deny writes to it): `{schema: 1, id, purpose, enabled, started_at (UTC), stop_at (UTC), stop_at_source, timezone: "Europe/London", overrides: {lane_slots, daily_spend_cap_usd}, restart_after_stop: false}`. While enabled and before stop_at, its overrides win over config.json; after stop_at (or when disabled) config.json applies again with no edit needed. Create the current campaign: stop_at 2026-11-04T18:00 Europe/London, source "the owner in chat, 2 Nov", overrides `{"lane_slots": {"claude": 3, "codex": 1}, "daily_spend_cap_usd": 400}`; and set config.json back to the normal values `{"claude": 1, "codex": 1}` and 5.0, so the sprint lives only in the campaign file.
2. `python orchestra/campaign.py status|pause|resume|stop|emergency-stop|disable-schedule`:
   - status: campaign, time left (London), slots, running tasks per slot, paused?
   - pause: no new dispatch (running tasks finish); resume: only within the deadline; stop: pause + mark the campaign ended; emergency-stop: also kill the running lane process trees the runner owns (from lock files / recorded PIDs — never unrelated claude/python/ollama processes) and park those tasks as "cancelled by the owner" with their branches kept.
   - A manual pause/stop persists in `.review/control.json` and nothing (watchdog, new tick, logon) undoes it; only `resume` does.
3. The runner checks pause/stop/deadline before every dispatch (`why_not_runnable` reason: "paused by the owner" / "campaign ended"), and uses UTC in state with London for display.
4. STATE NOW shows the campaign line ("Sprint: ends Wed 18:00, 1 d 22 h left, Claude ×3").
5. Tests with a fake clock and fake lane: overrides before/after stop_at, pause blocks dispatch but not a running task, emergency-stop kills only the owned tree (mock), resume refused after the deadline, malformed campaign.json → ignore it and log (never crash, never apply half of it).

Docs in `orchestra/README.md` (create or extend).

## Gate
```
.venv\Scripts\python.exe -m unittest orchestra.test_campaign -v
.venv\Scripts\python.exe -m unittest orchestra.test_runner
```
