---
role: planner
version: 1
inputs: one scout candidate with its evidence items, the gate registry (orchestra/gates.json: gate ids, what each runs, which areas may use it), the repo's areas, and the test files that exist
output: planner.schema.json, merged by code with the candidate's evidence into a task packet for orchestra/admission.py
may: choose a title, objective, scope paths, acceptance and one gate id + argument from the registry
may_not: write a shell command, choose a gate outside the registry, write files, queue anything (admission.py does that, and may refuse)
budget: one model call, local_team_timeout_s
model: local Ollama (local_team_model)
done_when: the answer validates; admission then checks paths, gate, premise, duplicates and rate
---
The system prompt is `role_prompts.planner` in `../rules.json`.
