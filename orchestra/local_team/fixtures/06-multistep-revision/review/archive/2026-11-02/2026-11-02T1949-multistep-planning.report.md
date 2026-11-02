RESULT: PASS
GATE: `unittest discover -s scripts` on test_dialogue_reliability.py (33 tests), test_brain_guards.py (1 test wrapping all its script checks) and test_planner_multistep.py (27 tests) -> all three exit 0; does not cover: live model behaviour (that is the A/B in docs/multistep_planning.md, which no gate re-runs), the real tray/voice path, eval/run_eval.py end to end (the fixture loader is unit-tested, the full harness isn't), or the rest of scripts/test_*.py, which I ran by hand (all pass except test_runtime_status, see ODD)
HUMAN: none
PREMISE: right. The roadmap's "honest gap" is real: as one turn, the control arm passed 4/18 clean and 3/18 live on the new multi-step cases. `planner.py` already exists, but it does deterministic study plans and briefings, not multi-step execution, so the new module is `multistep.py`.
DONE:
- multistep.py, the detector: code only. A request counts when it splits into 2 or more verb-led parts on and/then/commas and has a tool_router keyword. Unit-tested, including that none of the 55 existing eval prompts triggers it.
- multistep.py, the plan: one JSON-schema call `{"steps": [...]}`, at most 5 steps. A step must be mostly his words, with one step per detected part; otherwise his own parts become the plan.
- multistep.py, execution: each step goes through the existing Brain.respond tool loop as its own message. Each step is routed by tool_router (with the step before it, for "...from it") and capped at what the whole request routes to. Earlier steps arrive as short summaries in that call's system context only. SYSTEM_PROMPT is unchanged.
- Retry: a tool error, or the honesty net rejecting a claim, gets exactly one retry, with the error shown and that exact call refused (new `_step_banned` check in brain.execute). A second failure stops the plan and says plainly what failed and what's not done. A decline is never retried. A mutating call that worked is never repeated by a later step. Permission guards (add_task, write_file, due) check his original words.
- brain.py: small hooks only. The planner hook runs before the tool loop. The step tool set is applied. The honesty-net replies are now constants (UNCONFIRMED_REPLY, FAILED_REPLY). Delegation is skipped inside a step.
- 6 eval cases (`multi_*`) in eval/cases.py, the case list run_eval.py uses, with tool fixtures. run_eval gained per-case `fixtures` and an `all_tools_of` check.
- A/B with Ollama live, 3 rounds per arm, alternating, full 61-case suite, clean and live. New cases: control 4/18 clean, 3/18 live; planner 13/18 clean, 14/18 live. Old suite: planner 55/55 in every round, both conditions. Planner kept (default on, DENISSE_PLANNER=0 is the control). Tables and failure analysis are in docs/multistep_planning.md.
NOT DONE:
- multi_signals_revision is still 0/3 in both arms. Step 2 writes a generic revision list without reading the file step 1 found. The likely fix is to read the found file in code; it's described in the doc, not built.
- multi_tasks_timer is 3/6 in the planner arm: the timer reply sometimes drops "25" or the task names.
- The vault roadmap was not updated (the task didn't ask; the vault is outside this worktree).
ODD:
- test_brain_guards.py ended with a bare `sys.exit()` at import, so `unittest discover` always reported an import error (exit 1, even on ALL PASS). That made gate line 2 unpassable before any change of mine. I wrapped the existing checks in one TestCase that asserts none failed, and sys.exit now runs only under `__main__`. No check was changed. It's in its own commit, f97a7f5, for the runner to review.
- scripts/test_runtime_status.py fails 1 of 12 ("code version is the checked-out commit"). In a worktree `.git` is a file, not a directory, so this is unrelated to this change and not in the gate.
- The first live runs showed problems that the unit tests couldn't. Example wording in the plan prompt was copied into unrelated plans. Policy refusals were being counted as step failures. Failure text leaked tool names. All three were fixed before the A/B (0875a96); the A/B ran on frozen code.
- An invalid model plan now falls back to the detector's own parts, not to a single turn. The model still writes the plan when its plan passes the checks.
- For the live condition I copied the main checkout's memory/*.json into this worktree's memory/ (read-only, gitignored), the same as the 2026-11-02 tool-reflex A/B. Eval reports went to a temp folder, not the vault, and nothing was added to the dataset.
- The Monitor tool was refused (no approval surface), so I waited on the A/B with background polling loops instead.
- Some smoke runs took 200–370 s per case, apparently from GPU contention with another session. The A/B itself ran at normal speed (3–6 min per round).
COMMITS: f97a7f5, a8c391f, d74a37b, 0875a96, 1a3f924
