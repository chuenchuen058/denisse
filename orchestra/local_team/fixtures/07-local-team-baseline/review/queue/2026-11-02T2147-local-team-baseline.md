# Local team: measure what llama3.1:8b proposals are worth before trusting them
Queue: auto
Priority: normal
Weight: heavy
Kind: work
Lane: claude
Written: 2026-11-02T21:47

The local agent team and producer landed today (`orchestra/local_team/`, `orchestra/admission.py`; report `.review/archive/2026-11-02/2026-11-02T2002-local-team-producer.report.md`). Its NOT DONE: no measured quality baseline for llama3.1:8b proposals beyond one smoke run, and section 9 steps 6-7 (dependency/resource/risk estimates, re-checking stale premises before dispatch) aren't done.

1. Build a fixture set of 12 scout inputs from real material in this repo (copy into `orchestra/local_team/fixtures/`, scrubbed of anything personal): parked-task why files, NOT DONE sections from today's archived reports, a failing test output, a noisy log with nothing actionable (expected: no candidate).
2. For each, write the expected outcome by hand (candidate yes/no, area, a valid gate from `orchestra/gates.json`).
3. Run scout → planner → admission on all 12 with the real local model, 3 rounds; score: precision of candidates, admission pass rate, duplicates caught, bad gates rejected, latency, GPU lease respected. Report in `orchestra/local_team/BASELINE.md`.
4. Add the "re-check premise before dispatch" step: admission records the evidence fingerprint; `orchestra/runner.py`'s why_not_runnable refuses a local-team task whose evidence file changed since admission ("premise moved, re-check") — only for tasks with the sidecar.
5. Leave auto-admit off; recommend in BASELINE.md which area (if any) has earned it.

## Gate
```
.venv\Scripts\python.exe -m unittest discover -s orchestra/local_team -p "test_*.py" -v
.venv\Scripts\python.exe -m unittest orchestra.test_admission -v
.venv\Scripts\python.exe -m unittest orchestra.test_runner
```
