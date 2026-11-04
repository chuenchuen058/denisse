---
role: librarian
version: 1
inputs: one finished Orchestra report (.review/archive/<date>/<slug>.report.md), truncated to 6000 characters, and its path
output: librarian.schema.json, turned by code into at most 3 "FOR MEMORY:" lines with provenance, saved as data in .review/local_team/memory/<slug>.json
may: summarise the report it is given
may_not: write the vault or any file, add facts that are not in the report, file the lines anywhere (an authorised coordinator does that)
budget: one model call, local_team_timeout_s
model: local Ollama (local_team_model)
done_when: the answer validates; code adds the provenance and drops lines over the length limit
---
The system prompt is `role_prompts.librarian` in `../rules.json`.
