# Multi-step planner: use what step 1 found, keep the details the user gave
Queue: auto
Priority: normal
Weight: heavy
Kind: work
Lane: claude
Written: 2026-11-02T21:45

Follow-up to today's multi-step planner (merged; see `docs/multistep_planning.md` and its archived report `.review/archive/2026-11-02/2026-11-02T1949-multistep-planning.report.md`). Two eval cases stay red:
- `multi_signals_revision` 0/3 in both arms: step 2 writes a generic revision list without reading the file step 1 found.
- `multi_tasks_timer` 3/6 in the planner arm: the timer reply sometimes drops the "25" or the task names.

Fix through code, not prompt growth: (a) when a step's result names a file path, the next step that produces content gets that file's text (bounded, e.g. first 4000 chars, via the existing safe read tool) in its step context; (b) carry the user's literal numbers and names into the step that needs them (extract them in code from the request, pass as structured fields), and have the honesty net check the final reply contains them. Then re-run the A/B (3 rounds, current vs fixed) on the full suite plus the multi-step cases, keep the change only if those two improve and nothing regresses in a majority of rounds, and update `docs/multistep_planning.md` with the new table.

Put new test modules in scripts/ next to the existing ones (the gate discovers them there).

## Gate
```
.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_planner_multistep.py" -v
.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_dialogue_reliability.py" -v
```
