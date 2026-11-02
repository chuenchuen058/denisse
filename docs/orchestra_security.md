# Orchestra: what keeps a task session in its lane

Orchestra (`orchestra/runner.py`) runs queued tasks unattended, each in its own git
worktree under `<repo parent>/<repo name>-wt/`, and merges a PASS into this public repo.
These are the layers that stop a session from acting outside its worktree, what each one
covers, and what none of them cover. They are guard rails, not a sandbox.

## Layers

| Layer | Where | Lanes | What it does |
|---|---|---|---|
| Guard hook | `orchestra/hooks/guard.py`, passed to `claude -p --settings` as a PreToolUse hook | Claude only | Denies Write/Edit/MultiEdit/NotebookEdit whose target, **after `os.path.realpath`**, is outside the worktree (so a write through the `.venv` junction into the main checkout is denied). Denies Bash commands that run `git push`, `git -C`, `git reset --hard`, `git clean -f`, `gh`, recursive deletes, `git worktree remove/prune`, or name the main checkout's path. Logs every call to `<worktree>/.review/hook.log`; fails open on its own errors. |
| Hook-loaded check | `runner.py` (`hooks_loaded`) | Claude only | A PASS from the Claude lane is not merged if `.review/hook.log` doesn't exist, i.e. the guard never ran. |
| pre-push hook | tracked `orchestra/hooks/pre-push`, installed by the runner into the main `.git/hooks/pre-push` at the start of every tick (if missing or different; logged when it changes) | All lanes | Worktrees share the main hooks folder. Refuses a push when `ORCHESTRA_WORKTREE` is set, or when `git rev-parse --show-toplevel` is under the worktrees folder (so clearing the env var doesn't help). Pushes from the main checkout are allowed. |
| Runner gate re-run | `runner.py` | All lanes | The session's PASS is not trusted: the runner re-runs the task's gate commands in the worktree and merges only if all exit 0. |
| Main-checkout change check | `runner.py` (`main_checkout_status`) | All lanes | A PASS is not merged if `git status --porcelain` of the main checkout changed during the run (`.review/` excluded); uncommitted main-checkout changes to files the branch touches block the merge. |
| Aftercare push checks | `orchestra/aftercare.py` (`push`, `privacy_problems`) | All lanes (runs after a merge, from the main checkout) | Pushes only the default branch, only with no merge/rebase in progress, only fast-forward of `origin`, and refuses if the outgoing diff touches private-looking paths (vault folders, `memory/`, `.review/`, `.env`, keys, secrets) or adds a line that looks like a key or token. |

## Per lane

- **Claude:** guard hook + every all-lanes layer. The CLI's `--disallowedTools` also lists
  `Bash(git push:*)` and `Bash(gh:*)`.
- **Codex:** no tool guard. It runs with `-s danger-full-access` (its Windows sandbox can't
  start a shell when launched in the background; Chuen's choice, 2 Nov 2026). Only the
  all-lanes layers apply.
- **Gemini:** `--approval-mode auto_edit` (edits, no shell commands); no Orchestra tool
  guard. Only the all-lanes layers apply.

## Not covered

- **Code run by an allowed command.** The guard reads tool inputs, not what they do:
  `python -c`, a script the session wrote, or a test can write anywhere the user can.
  Running `.venv\Scripts\python.exe` is allowed on purpose (gates need it).
- **Git-ignored files in the main checkout.** The main-checkout change check uses
  `git status`, which doesn't see ignored paths such as `.venv/`. For the Claude lane the
  guard's realpath check stops Write/Edit there; for other lanes, and for commands,
  nothing does.
- **Anything outside the repo** (the vault, the home folder, other projects) for the Codex
  lane, and for any lane via commands.
- **Pushing without git's hooks.** `git push --no-verify` skips pre-push; the guard denies
  `git push` in any form for the Claude lane, but the Codex lane is not stopped from it.
  Network tools other than git (e.g. `curl` to the GitHub API) are not checked by any layer.
- **Bash pattern matching** is textual. The guard catches the listed commands at a command
  position; it is not a shell parser, so a command spelled differently from the patterns is
  not matched.
- **Fail-open.** If `guard.py` itself errors, the call is allowed (and logged to
  `hook.log`), so a broken guard doesn't stall a run.
