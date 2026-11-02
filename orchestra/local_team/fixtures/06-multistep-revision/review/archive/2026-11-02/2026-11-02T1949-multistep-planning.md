# Denisse: plan multi-step requests and retry with a different approach
Queue: auto
Priority: normal
Weight: heavy
Kind: work
Lane: claude
Written: 2026-11-02T19:49

Roadmap (vault: Denisse/roadmap.md, "The honest gap"): self-verification is mostly closed; what's left is **planning for multi-step requests** and **retry with a different approach**. Example requests that fail today in spirit: "find my signals coursework, make a revision list from it and remind me on Thursday", "move this week's lecture PDFs into the right module folders and tell me what's missing".

Constraints (from the repo's own lessons, read `docs/adaptive_brain.md`, `tool_router.py`, `brain.py` first): an 8B local model (`llama3.1:8b`, 8192 context); the prompt budget rule (tools are routed, not all offered); improvements must act through code and tool routing, not by growing the system prompt; measure with the eval harness, run pass^k (3 rounds) because one run is noise.

Build:
1. A small planner step used only when a request has 2+ distinct actions (cheap detector in code: conjunctions + multiple verbs/tool cues; unit-tested). The model writes a numbered plan of ≤5 steps as JSON (one short schema), the code executes steps one at a time through the existing tool loop, each step seeing only the tools routed for it and the previous steps' results summarised.
2. Retry: when a step's tool returns an error or the honesty net rejects a claim, one retry with the error shown and that exact tool call banned; then stop and tell the owner plainly what failed. Never loop.
3. 6 new eval cases for multi-step requests with fake tools/fixtures, added to the eval dataset the harness uses.
4. A/B: current vs planner, 3 rounds each, on the full suite plus the new cases. Keep the planner only if the new cases improve and the old suite doesn't regress (majority of rounds). Report the table in `docs/multistep_planning.md`. If Ollama isn't reachable, build and unit-test everything, skip the A/B and say so under NOT DONE.

Put new test modules in scripts/ next to the existing ones (the gate discovers them there).

## Gate
```
.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_dialogue_reliability.py" -v
.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_brain_guards.py" -v
.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_planner_multistep.py" -v
```
