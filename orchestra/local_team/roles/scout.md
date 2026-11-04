---
role: scout
version: 1
inputs: evidence items collected by code since the last successful scan (runner.log lines, parked why-files, NOT DONE / ODD bullets of archived reports, failing suites in .review/checks.json); each has an id, a source path, a time and a text
output: scout.schema.json
may: read the evidence it is given
may_not: read or write files, run commands, invent evidence ids, propose work with no evidence
budget: one model call, local_team_timeout_s
model: local Ollama (local_team_model)
done_when: the answer validates and every evidence id it cites was given to it
---
The system prompt is `role_prompts.scout` in `../rules.json`.
