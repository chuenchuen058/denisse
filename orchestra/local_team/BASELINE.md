# Local team baseline: what llama3.1:8b proposals are worth

Measured 2 Nov 2026 on `llama3.1:8b` (Q4_K_M, Ollama, `num_ctx` 8192, temperature 0),
scout v1 and planner v1, admission as of commit 870e241. Two full runs of 3 rounds × 12
fixtures each: 72 fixture runs, 204 model calls.

    .venv\Scripts\python.exe -m orchestra.local_team.baseline --rounds 3

Raw results: `baseline_runs/2026-11-02T221748-run1.json` (22:05–22:17, GPU shared with
another session's eval) and `baseline_runs/2026-11-02T223039-run2.json` (22:19–22:30, GPU
free). Harness: `baseline.py`. Fixtures: `fixtures/`.

**Bottom line: the scout finds every real gap but cannot tell a gap from a note, and the
planner can't scope.** About 1 admitted draft in 5 is a task worth doing. No area has
earned auto-admit. Keep `auto_admit_areas: []`.

## The fixtures

Twelve scout inputs copied from real Orchestra material, scrubbed of names and private
paths. Each has a hand-written `expected.json`: candidate yes/no, area, the gates from
`gates.json` that would be right, and whether an existing task already covers it.

| # | Fixture | Source | Expect |
|---|---|---|---|
| 01 | parked-generic-why | parked why file ("FAIL: a blind re-run won't change it") | no candidate: it gives no cause |
| 02 | hub-jobs-pruning | hub-async-jobs report + the archived 2008 task that fixed both gaps | candidate, orchestra, **duplicate** |
| 03 | orphan-lane-timeout | crash-reconcile NOT DONE | candidate, orchestra |
| 04 | naive-timestamps | campaign-controls NOT DONE | candidate, orchestra |
| 05 | failing-atrium-suite | checks.json: the two atrium suites that failed on import (test-suite-green run) | candidate, atrium |
| 06 | multistep-revision | multistep-planning NOT DONE + the parked follow-up task | candidate, app, **duplicate** |
| 07 | local-team-baseline | producer NOT DONE + this task, queued | candidate, orchestra, **duplicate** |
| 08 | noisy-log-resolved | runner.log: PARTIAL/PARKED, then REQUEUED and PASS + merged | no candidate |
| 09 | noisy-log-no-report | runner.log: NO_REPORT / codex not on PATH, then PASS + merged | no candidate |
| 10 | human-only | global-hotkey + power-aware-dispatch: press a key, re-register Task Scheduler | no candidate |
| 11 | done-as-asked | "None within scope", "not performed, as requested", "as instructed" | no candidate |
| 12 | needs-real-data | no real limit output captured; old history can't be backfilled | no candidate |

Labelling 02 was itself a lesson. I first marked the pruning gap as new. Admission refused
it as a duplicate, and it was right: the 2008 task's title says "old job files get pruned".

## Scores

"Packets" are planner answers handed to admission (at most 2 per fixture, the best
non-low candidates, as `produce` does).

| Measure | Run 1 | Run 2 |
|---|---|---|
| Fixture accuracy (candidate yes/no right) | 50% (18/36) | 50% (18/36) |
| Positive fixtures with ≥1 candidate (recall) | 18/18 | 18/18 |
| Negative fixtures left quiet | **0/18** | **0/18** |
| Candidates | 144 | 147 |
| Candidate precision, automatic (right fixture + right area) | 42% | 42% |
| Candidate precision, **by hand** (run 1, round 1: real, un-done gap) | **10/46 = 22%** | – |
| Packets planned / planner answers outside schema | 66 / 0 | 66 / 0 |
| Admitted (all as `Queue: draft`) | 10 (15%) | 11 (17%) |
| Refused: path / gate | 52 / 4 | 49 / 6 |
| False admissions (admitted from a no-candidate fixture) | 3 | 3 |
| Admitted with a scope file that doesn't exist | 3 | 4 |
| Admitted drafts worth doing, by hand | 3/10 | 1/11 |
| Duplicate fixtures: packets / reaching dedupe / caught | 18 / 4 / **0** | 18 / 7 / **0** |
| Gate the planner chose was one of the expected ones | 26% | 30% |
| Latency, scout (median / max) | 6.9 s / 10.9 s | 6.5 s / 9.6 s |
| Latency, planner (median / max) | 6.3 s / 8.3 s | 6.1 s / 7.4 s |
| Lease held at every `/api/chat` request and answer | not measured | **102/102** |
| Lease, once taken, held without a gap until released | see note | **102/102** |
| Lease released after every call | 102/102 | 102/102 |
| Probe: lease held by another holder → scout skips, no model call | 3/3 | 3/3 |

Rounds within a run are close but not identical, even at temperature 0. For example, fixture
09 gave 2 candidates in round 1 and 5 in round 2. Treat single-round results as noisy.

Lease note: run 1's harness reported "lease not held" on 65 planner calls. That was the
harness, not the lease. Its sampler started before `ask()` took the lease, because
`ready()` first re-samples nvidia-smi for 2 s after our own previous call. Run 2 checks at
the HTTP request itself and checks that the held samples form one unbroken run: clean.

## What goes wrong, in order of cost

1. **The scout reads report notes as defects.** All 18 negative fixture runs per run
   raised candidates, mostly marked `high`. ODD bullets are decisions already taken
   ("`resume` is refused after the deadline, and I added `resume --after-deadline`") and
   come back as bugs ("The `resume` command is refused after the deadline"). "Not done, as
   the task said" comes back as "X was not done". A FAIL line with no cause becomes "a
   problem with the task itself". On the positive fixtures, about half of each fixture's
   candidates were real. The rest were ODD notes misread the same way.
2. **The planner can't scope.** 42 of 66 run-1 packets scoped the folder `orchestra/`.
   Others scoped `.review/...`, `orchestra/config.json`, or test files only. Admission
   refused them all, correctly. Most of the rest invent files (`orchestra/planner.py`,
   `orchestra/timer.py`, `orchestra/test_brain_guards.py`). Admission lets those through,
   because it checks that the folder exists, not the file.
3. **Dedupe never fired on a real duplicate (0/11 that reached it).** The model rewords
   the problem ("Read revision file in multi_signals_revision" against the parked "Multi-step
   planner: use what step 1 found…"; "split_tasks is listed in the task" against "split_tasks
   runs as a background job too…"). Scopes are invented, so file overlap doesn't match either.
   Dedupe runs after the path check, so most duplicate-fixture packets (11 of 18 in run 2)
   were refused for their path before dedupe saw them. That is safe, but it means dedupe is
   barely exercised.
4. **Gates.** Admission caught every bad gate: a nonexistent `orchestra.test_importer`, a
   file path given as a module, and `atrium` as a module. But even the valid gates were
   mostly the reflexive `orchestra.test_runner`: 26–30% matched a right gate. In fixture 06
   it was attached to a brain/multistep change it doesn't test.
5. **Area hints are weak.** Almost every candidate says `orchestra`, including the atrium
   suite failure and the hotkey/tray change.

The one consistently good result is fixture 05 (a failing suite in checks.json). It got the
right problem, the right two files and the right gate (`unittest atrium.test_atrium`), and
was admitted in 4 of 6 rounds. The other 2 rounds used a malformed gate argument that
admission refused. Structured, unambiguous evidence works. Prose from reports does not.

What held: the planner never broke its schema (132/132). Every model call held the GPU lease
and released it. No call ran while another holder had the lease. Nothing was auto-queued.
Latency is low: about 6–7 s a call, about 20 s for a fixture's scout and 2 plans.

## Recommendation on auto-admit

**No area has earned it.** With the rules as they stand, auto-admit would have queued 21
tasks over these 72 runs. 6 came from fixtures that should have produced nothing, 7 have an
invented scope file, and every one of the 11 that reached dedupe from a covered fixture
was a duplicate it missed. The nearest candidate is "a failing suite in checks.json →
fix it", which is the atrium/scripts kind of task. But that is one fixture's behaviour,
not an area's. Re-measure before trusting it.

What would most likely move the numbers (not done here; each is its own task):

- Don't feed ODD bullets to the scout, or label them "decision already taken, not a
  defect". Feed NOT DONE only, and drop bullets that say "as asked/as instructed/the task
  said not to". This is deterministic code in `roles.collect`, not a prompt hope.
- Admission: refuse a scope file that doesn't exist unless it sits beside an existing file
  and is a test (a new test is legitimate; `orchestra/planner.py` is not).
- Planner: give it candidate file lists (files named in the evidence plus their tests)
  and make `scope_paths` an enum of those, so it can't invent or pick folders.
- Dedupe: also compare the packet's *evidence text* against existing task bodies, not
  only titles and files.
- Then re-run this baseline. Any change to `roles/*.md` or admission should re-run it
  and compare against these two runs.

## Premise re-check before dispatch (done in the same task)

Admission now records `evidence_files` (size + sha1 of each evidence file) in the sidecar
`.review/local_team/packets/<slug>.json`. `runner.why_not_runnable` refuses a task that
has a sidecar if any evidence file is gone or its first `size` bytes changed: "premise
moved, re-check". Appending to a log is not a change. Tasks without a sidecar are
untouched. Delete the sidecar to release a re-checked task.
