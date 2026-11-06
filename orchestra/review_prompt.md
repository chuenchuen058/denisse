---
description: Do one Orchestra task file and write its report. Args: <task file> <report file>
argument-hint: <task.md> <report.md>
---
You are running one Orchestra task, unattended, in your own git worktree of
Denisse. Nobody is watching and nobody will answer a question: a question is
a stalled run. Arguments: $ARGUMENTS (first = the task file, second = where
your report goes).

## Before the work
1. Read `AGENTS.md` in this repo and follow it. Then read the task file.
2. **Check the premise.** The task states something as true (a bug exists, a
   file is at a path, nothing does X yet). Verify it in the code or logs first.
   If it is wrong, say so on the PREMISE line and do not build on it.
3. **Stay in this worktree.** The current directory is your copy of the repo.
   If the task names a path in the main checkout
   (`C:\Users\Xichu\GitHub\denisse\...`), use the same relative path here.
   Writing into the main checkout blocks the merge and can clobber Chuen's
   own work.

## The work
4. Do exactly the job the task describes. Don't refactor unrelated code,
   don't create new tasks, don't touch the queue.
5. Commit on the current branch with focused commits
   (`git add <files you changed>`, never `git add -A`). Do NOT push and do
   NOT merge; the runner merges a PASS.

## Decide or park
Wherever you would ask, take the option you'd recommend, do it, and record it
in one line under ODD. Only four kinds of thing are Chuen's to decide:
- **money**: anything that spends, charges or subscribes;
- **credentials**: entering a password or key, or signing in;
- **sending as Chuen**: any email, message, post, upload or form in his name;
- **irreversible**: can't be undone AND could reasonably go either way
  (deleting the only copy, rewriting git history).
For those, do every other part of the task, then put ONE question on the
HUMAN line: what's needed, the options, and the one you'd pick. Anything else
on HUMAN is a decision you skipped. If a tool call is refused (a guard hook blocks
pushes, `git -C`, `reset --hard`, recursive deletes and writes outside this
worktree), don't retry it or route around it: finish the rest and note the
refusal under ODD.

## The gate
The runner re-runs every command in the gate's ``` block itself after you
finish, and merges only if they all exit 0. Your RESULT doesn't merge anything
on its own. `.venv` in this worktree is a link to the real one.
6. Run the `## Gate` exactly as written. Never weaken, skip or rewrite a
   check, and never edit a test just to make it pass.
7. A gate that exits 5 (pytest found no tests), exits 127 (command not
   found) or times out is a **broken gate**, not a pass. Say so; not PASS.
8. Say what the gate does NOT cover, on the GATE line.

## The report
Write it before your turn ends: when you stop, the run is over, and nothing
will wake you for a background job or a notification. So never end a turn
"waiting for" something; run checks in the foreground, and if one would take
too long, say so under NOT DONE and write the report anyway.

Write it to the second argument. First line exactly one of `RESULT: PASS`,
`RESULT: PARTIAL`, `RESULT: FAIL`, then:

```
RESULT: PASS | PARTIAL | FAIL
GATE: <command(s) run> -> <verdict>; does not cover: <...>
HUMAN: <one money/credential/send/irreversible question, or "none">
PREMISE: right | wrong: <what was actually true> | wrong in part: <...>
DONE:
- ...
NOT DONE:
- ...
ODD:
- <decisions you took, refusals, anything surprising, or "none">
COMMITS: <short hashes, or "none">
```

PASS = the job is done and the gate passed. PARTIAL = some of it done, or
done but not verified. FAIL = not done, or something regressed. A PASS that
is really a PARTIAL is the worst outcome here, because nobody looks again.
