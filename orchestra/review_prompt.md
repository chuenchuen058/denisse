---
description: Do one Orchestra task file and write its report. Args: <task file> <report file>
argument-hint: <task.md> <report.md>
---
You are running one Orchestra task, unattended, in your own git worktree of
Denisse. Nobody is watching. Arguments: $ARGUMENTS (first = the task file,
second = where your report goes).

1. Read `AGENTS.md` in this repo and follow it. Then read the task file.
2. Do exactly the job the task describes, nothing beyond it. Don't refactor
   unrelated code, don't create new tasks, don't touch the queue.
3. Commit your work on the current branch with focused commits
   (`git add <files you changed>`, never `git add -A`). Do NOT push and do
   NOT merge; the runner merges a PASS.
4. Run the `## Gate` exactly as written. Never weaken, skip or rewrite a
   check to make it pass. If you can't run it, the result is not PASS.
5. Never send anything as Chuen (email, messages, uploads, forms) and never
   spend money. If the job needs that, stop and put it on the HUMAN line.
6. Write the report to the second argument. Its first line must be exactly
   one of `RESULT: PASS`, `RESULT: PARTIAL`, `RESULT: FAIL`, then:

```
RESULT: PASS | PARTIAL | FAIL
GATE: <the command(s) you ran> -> <what they printed / the verdict>
HUMAN: <what only Chuen can do, in one line, or "none">
DONE:
- ...
NOT DONE:
- ...
ODD:
- <anything surprising, or "none">
COMMITS: <short hashes, or "none">
```

PASS = the gate passed and the job is done. PARTIAL = some of it done, gate
not fully passed. FAIL = not done. When unsure, it is not PASS.
