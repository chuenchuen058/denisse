# Eval speed — 2026-11-03

Task: make `eval/run_eval.py` fast enough to run 3 rounds on every change,
without changing what it measures. Profile first, then speed up, then show
pass^k per case is the same under the old and the new harness.

## Premise, checked first

The task said one round "takes long enough that sessions run 1 round, or
skip it". Measured on this machine with the GPU otherwise idle, before any
change:

| round (old harness, 83 cases × clean + live) | wall |
|---|---|
| 1 | 290 s |
| 2 | 270 s |
| 3 | 278 s |

So a full round is about 4½ minutes and 3 rounds about 14. The 2 Nov A/B in
`docs/tool_reflex_ab_2026-11.md` ran six full rounds of the then 55-case
suite in 11 minutes. The "17–121 s to first token" in `docs/voice_latency.md`
was the GPU being at 100% from *other* work, not the harness.

Two of the task's suggested levers were already in place:

- **The model is already warm.** `brain.py` calls Ollama with
  `keep_alive=-1`; `ollama ps` shows `llama3.1:8b` resident "Forever". A
  probe of six calls measured `load_duration` at 0.00–0.01 s on every one.
- **Ollama's prompt cache already hits.** Denisse's system prompt (3849
  chars) and 15 style examples are a fixed prefix on every call. The same
  1650-token request sent three times: prefill 0.74 s, then 0.02 s, then
  0.02 s. A different tool set with the same text: 0.26 s (the shared
  system prefix is reused, the tool block is not). The router's per-turn
  tool set is part of what the suite measures, so it is left alone.

So the premise is wrong in part: the harness had nothing left to warm up,
and the real problems were the ones the task's last two bullets name — the
GPU being shared with something else, and no quick way to run less than the
whole suite.

## Where a round's time goes

Measured by the new always-on profile (Ollama's own `load_duration`,
`prompt_eval_duration`, `eval_duration` per call, plus a clock around every
tool call), full suite, GPU idle at start:

| segment | round 1 | round 2 | round 3 |
|---|---|---|---|
| whole run (wall) | 258 s | 337 s | 287 s |
| startup: imports, brain, scratch state | 2.1 | 14.8 | 10.1 |
| model, total wall | 244.6 | 282.2 | 263.8 |
| &nbsp;&nbsp; load | 1.9 | 1.8 | 1.8 |
| &nbsp;&nbsp; prompt prefill | 67.1 | 72.6 | 71.3 |
| &nbsp;&nbsp; generation | 169.3 | 202.2 | 183.7 |
| tools | 9.4 | 37.9 | 10.9 |
| python between calls | 2.2 | 2.0 | 2.0 |
| model calls | 294 | 292 | 290 |
| prompt tokens in | 464,065 | 461,578 | 457,659 |
| tokens generated | 7,745 | 8,108 | 7,575 |
| longest reply (tokens) | 451 | 450 | 451 |
| tool calls | 122 | 121 | 119 |

Reading it:

- **The round is the model.** 85–95% of the wall is inside Ollama, and
  two thirds of that is generation: ~7,800 tokens a round at ~45 tokens/s
  on this GPU. The Python around the calls is 2 s. There is no harness
  overhead left to remove.
- **Load is nothing** (1.8 s over ~290 calls; the model stays resident).
- **Prefill is ~70 s a round, ~0.24 s a call, over 460k prompt tokens.**
  The cache covers the system prompt; what is re-evaluated each call is
  the tool block (different per case, by design of `tool_router`) and the
  style examples and messages after it. Putting the examples ahead of the
  tools would let the cache cover them too, but it changes the prompt the
  model sees, which is what the suite measures. Not done.
- **Tools are 9–11 s on a quiet round and 38 s on round 2**, all of the
  extra in `find_files` (24 s against 3–4 s), which walks his real
  OneDrive folders. That spread, plus startup swinging from 2 s to 15 s
  (disk cache after the first round), is the whole difference between
  rounds. The network tools (`get_weather`, `get_news`) are under 1.5 s.
- **The slowest cases are the long replies and the multi-step plans**:
  `windows_paths` (~350 tokens, 10 s), `multi_signals_revision` (8 model
  calls, 11 s), `find_passport` (7 calls), `multi_lecture_pdfs` (5–6
  calls). They are slow because of what they ask, not how they are run.

## What changed

All in `eval/speed.py` (new) and `eval/run_eval.py` / `eval/run_pass_k.py`.
Nothing in `brain.py`: the brain's calls, prompts, options and tool sets are
exactly what they were.

- **Profile in every report.** Each report now ends with "Where the time
  went": wall, startup, model (load / prefill / generation), tools, the
  Python between calls, the learning pass; token counts and the slowest
  tools. Each case result carries its own slice (`profile`), and `--json
  PATH` writes everything.
- **GPU lease is a gate, not a note.** `run_eval.py` and `run_pass_k.py`
  already took `.review/gpu.lease`; if it was still held after 5 minutes
  they printed a note and ran anyway — against another model job. Now they
  exit 3 (the scheduled runner's own "skipped, not a run" code) and say who
  holds it. `--wait-gpu SECONDS` waits longer, `--force` restores the old
  behaviour. `scripts/model_bakeoff.py` keeps the old behaviour (it calls
  `_gpu_lease()` with the defaults).
- **GPU utilisation recorded at start.** Two `nvidia-smi` samples half a
  second apart after the brain is up and before the first case, plus the
  models `ollama ps` has loaded. The report header shows it; at or above
  30% busy (`DENISSE_EVAL_GPU_BUSY_PCT`) the report carries a **Noisy run**
  callout and the summary line says `NOISY`, so a slow round is never read
  as evidence against Denisse.
- **`--changed-only REF`.** Runs the cases whose code paths a change since
  `REF` can reach, plus a fixed 13-case smoke set (one per path the brain
  can take: code-answered greeting, plain model reply, core tool, files,
  tasks, network tool, facts, notes, planner, study mode, multistep,
  desktop). Case → module is decided without a model, from the prompt: the
  non-core tools `tool_router` would offer it, the tools it asserts, and
  the brain's own pre-model matchers (`_BRIEFING`, `_STUDY_BRIEF`,
  `study_mode.match_command`, `multistep.is_multistep`, …). It is
  conservative on purpose: a change to anything on every turn's path
  (`brain.py`, `tool_router.py`, `tools.py`, `eval/cases.py`, …) or to a
  module brain imports that no case maps to is the full suite. A subset run
  writes its report to a temp folder, never curates `dataset.jsonl` and
  skips the learning pass. Examples:

  | changed | cases |
  |---|---|
  | nothing | 13 (smoke) |
  | `timers.py` | 16 |
  | `desktop_context.py` | 23 |
  | `planner.py` | 25 |
  | `tray.py` (not on the brain's path) | 13 |
  | `brain.py` | 83 |

  `run_pass_k.py --rounds 3 --changed-only REF` is the quick pass^3; it
  also gained `--all`.

## What was measured and not kept

- **`--jobs N`.** Ollama is running with one parallel slot (`ollama ps`
  shows context 8192, i.e. 1 × `num_ctx`), so concurrent requests queue
  inside Ollama. Measured with 8 prompts through the real prefix and 8
  tools, GPU idle: 1 job 3.6 s and 4.3 s; 2 jobs 4.0 s and 4.3 s; 4 jobs
  4.6 s, with per-call latency 2–5× worse. Raising `OLLAMA_NUM_PARALLEL`
  would multiply the KV cache (the 8192 context already costs ~0.5 GB of
  the ~2 GB headroom on this 8 GB GPU; `brain.py` says why that headroom
  is kept). The brain is also not thread-safe (`_retrying`, `_plan_tools`
  and the eval's own monkeypatched tool table are per-process). No
  `--jobs` was built; the default stays 1 because parallel was not
  faster.
- **`num_predict` cap.** The longest reply in 876 calls over 3 rounds was 451
  tokens; the mean is 27. A cap low enough to save time would cut
  `windows_paths` and the multi-step plans, which are the replies those
  cases check, so it would change what is measured. A cap high enough not
  to (512+) saves nothing. No cap; the per-call `eval_count` is now in
  every report, so a runaway reply would show up as `longest reply`.

## Old vs new harness, 3 rounds each

Same worktree, same machine, one after the other (old 11:35–11:49,
new 11:49–12:04), GPU idle at the start of each round,
`--no-dataset`, reports to scratch. This worktree has no `memory/`, so
"live" here replays an empty history and the `recall_*` cases (which read
his real `facts.json`) fail in both arms — the comparison is between
harnesses, and both arms see the same thing.

| round | old wall | new wall |
|---|---|---|
| 1 | 290 s | 259 s |
| 2 | 270 s | 337 s |
| 3 | 278 s | 287 s |
| mean | 279 s | 294 s |

The new harness is not faster per round and was never going to be: the
profile above shows the round is the model. The spread (259–337 s) is
`find_files` and disk cache, present in both arms. A smoke-set run
(`--changed-only HEAD` with nothing changed, 13 cases × 2) took 44 s wall:
startup 2.4, model 38.2, tools 1.9.

pass^3 per case, both conditions (passes out of 3 rounds):

| case | old clean | old live | new clean | new live |
|---|---|---|---|---|
| `greet_hey` | 3/3 | 3/3 | 3/3 | 3/3 |
| `greet_thanks` | 3/3 | 3/3 | 3/3 | 3/3 |
| `identity` | 3/3 | 3/3 | 3/3 | 3/3 |
| `time` | 3/3 | 3/3 | 3/3 | 3/3 |
| `math` | 3/3 | 3/3 | 3/3 | 3/3 |
| `find_file` | 3/3 | 3/3 | 3/3 | 3/3 |
| `windows_paths` | 3/3 | 3/3 | 3/3 | 3/3 |
| `task_add` | 3/3 | 3/3 | 3/3 | 3/3 |
| `task_list` | 3/3 | 3/3 | 3/3 | 3/3 |
| `timer_start` | 3/3 | 3/3 | 3/3 | 3/3 |
| `timer_set` | 3/3 | 3/3 | 3/3 | 3/3 |
| `timer_stop` | 3/3 | 3/3 | 3/3 | 3/3 |
| `vault_deadline` | 3/3 | 3/3 | 3/3 | 3/3 |
| `vault_not_for_general` | 3/3 | 3/3 | 3/3 | 3/3 |
| `garbled` | 3/3 | 3/3 | 3/3 | 3/3 |
| `greet_morning` | 3/3 | 3/3 | 3/3 | 3/3 |
| `small_talk` | 3/3 | 3/3 | 3/3 | 3/3 |
| `time_elsewhere` | 3/3 | 3/3 | 3/3 | 3/3 |
| `weather` | 3/3 | 3/3 | 3/3 | 3/3 |
| `weather_follow_up` | 3/3 | 3/3 | 3/3 | 3/3 |
| `news` | 3/3 | 3/3 | 3/3 | 3/3 |
| `now_playing` | 3/3 | 3/3 | 3/3 | 3/3 |
| `system_status` | 3/3 | 3/3 | 3/3 | 3/3 |
| `recall_british` | 0/3 | 0/3 | 0/3 | 0/3 |
| `recall_loose` | 0/3 | 0/3 | 0/3 | 0/3 |
| `self_voice` | 3/3 | 3/3 | 3/3 | 3/3 |
| `stop_talking` | 3/3 | 3/3 | 3/3 | 3/3 |
| `shush` | 3/3 | 3/3 | 3/3 | 3/3 |
| `iq_question` | 3/3 | 3/3 | 3/3 | 3/3 |
| `self_parameters` | 3/3 | 3/3 | 3/3 | 3/3 |
| `news_stop` | 3/3 | 3/3 | 3/3 | 3/3 |
| `skills_named` | 3/3 | 3/3 | 3/3 | 3/3 |
| `find_passport` | 3/3 | 3/3 | 3/3 | 3/3 |
| `file_not_app` | 3/3 | 3/3 | 3/3 | 3/3 |
| `latest_cv` | 2/3 | 3/3 | 3/3 | 3/3 **≠** |
| `why_not_found` | 3/3 | 3/3 | 3/3 | 3/3 |
| `briefing_real` | 3/3 | 3/3 | 3/3 | 3/3 |
| `ack_no_clock` | 3/3 | 3/3 | 3/3 | 3/3 |
| `complaint_retries` | 3/3 | 3/3 | 3/3 | 3/3 |
| `what_retries` | 3/3 | 3/3 | 3/3 | 3/3 |
| `my_projects` | 3/3 | 3/3 | 3/3 | 3/3 |
| `internship_tracker` | 3/3 | 3/3 | 3/3 | 3/3 |
| `internship_plan` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_plan` | 3/3 | 3/3 | 3/3 | 3/3 |
| `handoff_cover_letter` | 3/3 | 3/3 | 3/3 | 3/3 |
| `handoff_research` | 3/3 | 3/3 | 3/3 | 3/3 |
| `handoff_solutions` | 3/3 | 3/3 | 3/3 | 3/3 |
| `local_reminder_not_handoff` | 3/3 | 3/3 | 3/3 | 3/3 |
| `follow_on_briefing` | 3/3 | 3/3 | 3/3 | 3/3 |
| `follow_on_plan` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_brief_signals` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_brief_em` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_brief_which` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_brief_answer` | 3/3 | 3/3 | 3/3 | 3/3 |
| `explain_this_error` | 3/3 | 3/3 | 3/3 | 3/3 |
| `explain_this_text` | 3/3 | 3/3 | 3/3 | 3/3 |
| `multi_signals_revision` | 3/3 | 3/3 | 3/3 | 3/3 |
| `multi_lecture_pdfs` | 0/3 | 0/3 | 0/3 | 0/3 |
| `multi_weather_umbrella` | 3/3 | 3/3 | 3/3 | 3/3 |
| `multi_tasks_timer` | 3/3 | 3/3 | 3/3 | 3/3 |
| `multi_tool_error` | 3/3 | 3/3 | 3/3 | 3/3 |
| `multi_news_plate` | 3/3 | 3/3 | 3/3 | 3/3 |
| `look_not_thats_great` | 3/3 | 3/3 | 3/3 | 3/3 |
| `look_not_this_is_fine` | 3/3 | 3/3 | 3/3 | 3/3 |
| `look_not_do_that_again` | 3/3 | 3/3 | 3/3 | 3/3 |
| `look_not_here` | 3/3 | 3/3 | 3/3 | 3/3 |
| `look_rewrite_this` | 0/3 | 1/3 | 0/3 | 0/3 **≠** |
| `look_translate_selected` | 3/3 | 3/3 | 3/3 | 3/3 |
| `look_summarise_copied` | 0/3 | 0/3 | 0/3 | 0/3 |
| `look_what_does_that_mean` | 2/3 | 3/3 | 1/3 | 1/3 **≠** |
| `vault_week_topic` | 3/3 | 3/3 | 3/3 | 3/3 |
| `vault_tutorial_pdf` | 3/3 | 3/3 | 3/3 | 3/3 |
| `vault_which_week` | 3/3 | 3/3 | 3/3 | 3/3 |
| `vault_lecturer` | 3/3 | 3/3 | 3/3 | 3/3 |
| `vault_miss_honest` | 3/3 | 3/3 | 3/3 | 3/3 |
| `vault_no_leak` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_start` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_timer_cycles` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_stop_clean` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_off_not_swallowed` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_keeps_module` | 3/3 | 3/3 | 3/3 | 3/3 |
| `study_no_invented_lecture` | 3/3 | 3/3 | 3/3 | 3/3 |
| `task_complete_one` | 3/3 | 3/3 | 3/3 | 3/3 |

Three cases differ, all three with the identical failure string in
both arms, and nothing in `brain.py` or the prompts changed between
arms — the model at temperature 0.2 is not deterministic:

- `latest_cv`: old 2/3 clean (round 2 said "rephrase"), new 3/3. One
  flaky reply in six.
- `look_rewrite_this`: old 0/3 and 1/3, new 0/3 and 0/3; every failure is
  `missing any of ['intern']`. It fails most rounds in either harness
  (also 0/2 in the smoke run). A real red case for the desktop-context
  work, not a harness effect.
- `look_what_does_that_mean`: old 2/3 and 3/3, new 1/3 and 1/3; every
  failure is `missing any of ['log', 'merge', 'sort']`. Same case, same
  failure, both arms; it was 2/3 in the first old round too.

`recall_british`, `recall_loose`, `multi_lecture_pdfs` and
`look_summarise_copied` are 0/3 in both arms. The first two read his real
`memory/facts.json`, which this worktree does not have (the nightly in the
main checkout had them green on 2 Nov). The other two were added on 3 Nov
and are red here in both arms; fixing them is not this task's job. What
matters for this comparison is that they are the same red under both
harnesses.

## How to use it

```
# the standard: full suite, 3 rounds, pass^k per case
.venv\Scripts\python.exe eval\run_pass_k.py --rounds 3 --all

# quick check of a change on a branch, since master
.venv\Scripts\python.exe eval\run_eval.py --changed-only master
.venv\Scripts\python.exe eval\run_pass_k.py --rounds 3 --changed-only master

# the nightly is unchanged: eval\run_scheduled.ps1 -> eval\run_eval.py --quiet
# (exit 3 now also means "GPU lease held", with the holder printed)
```
