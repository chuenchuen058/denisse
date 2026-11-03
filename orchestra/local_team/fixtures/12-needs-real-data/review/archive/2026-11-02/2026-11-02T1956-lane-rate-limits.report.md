RESULT: PASS
GATE: .venv\Scripts\python.exe -m unittest orchestra.test_runner -v -> 30 tests OK, exit 0 (orchestra.test_tools + test_aftercare also OK, 11 tests); does not cover: real Claude/Gemini limit output (those patterns come from known message formats, not captured logs), the parallel_lanes tick path with fallback (tests run the single-lock path because ORCHESTRA_REPO is set), the vault-note text for LIMITED/fallback runs (aftercare is off in tests), and time zones named in limit messages.
HUMAN: none
PREMISE: right — the main checkout's runner.log shows the Codex task going NO_REPORT -> PARKED after 2 attempts, and runner.py had no limit handling.
DONE:
- runner.py: one LIMIT_PATTERNS table (codex / claude / gemini) plus CODEX_LIMIT_FIXTURE, the real 2 Nov line. After a run with no report (NO_REPORT or TIMEOUT), detect_limit scans the last 40 lines of <slug>.lane.log. In claude stream-json only result/error events count, so a session that merely read about limits doesn't trip it.
- On a hit: logs `LIMITED <slug> lane=<lane> until <time>`, gives the attempt back (attempts -1), keeps the task queued, records state.json lanes.<lane> = {limited_until, since, message}. parse_limit_until handles "try again at 10:55 PM", "resets at 3am", "resets 3am (Europe/London)", "21:15", "in 20 minutes", "Nov 4th, 2026 9:05 AM", and claude's old "|<epoch>". If it can't parse a time, it uses now + 60 min.
- why_not_runnable returns "lane <x> limited until HH:MM" when the task can't fall back.
- lane_fallback in config.json (default {"codex": "claude"}): route() moves tasks without `Lane-Fixed: yes` onto the fallback lane, as long as that lane is configured and not limited itself. Its slots still apply because the task now really is on that lane. START is logged with "(fallback: lane codex limited until HH:MM)", the history entry gets fallback_from, and the vault note's lane field says "claude (fallback: lane codex was limited)". The header regex now accepts hyphenated keys like Lane-Fixed.
- save_state's parallel merge now carries the lanes dict too, so two lane runners don't drop each other's limits.
- state_now.py: a "**Limited lanes:**" line showing each limited lane, until when, and its fallback.
- Tests: Codex fixture -> LIMITED, attempts 0, still queued, 22:55, why text, STATE NOW; fallback to claude, while the Lane-Fixed task waits; an ordinary NO_REPORT is still an infra attempt; per-lane pattern table, including the read-about-limits case not counting; time parsing; unparseable -> +60 min; state merge keeps lane limits.
NOT DONE:
- No real Claude or Gemini limit output was captured to check those patterns against. They come from known message formats.
ODD:
- Also check TIMEOUT runs for a limit message, not only NO_REPORT: neither writes a report, and a CLI that hangs after printing its limit is still limited.
- Lanes not in the pattern table (custom lanes.json wrappers, the test "fake" lane) are checked against every lane's patterns.
- A time zone in the message (e.g. "(Europe/London)") is ignored; the time is read as local. A dated time already in the past falls back to +60 min.
- A run that hit its limit is still charged as usual (claude-lane runs get an estimate; codex shows "unknown").
- A LIMITED result still goes through the existing write_vault_note call, with the limit as "Why it stopped". That puts a short note in the vault for each limited run.
- The Bash tool rejected heredocs containing apostrophes, so all edits were made with the Edit tool. No guard refusals.
COMMITS: efa8570
