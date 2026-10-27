# Commits and the private vault backup

Chuen requested focused commits and a private vault backup on 27 October 2026.
After each coherent change and its relevant checks, commit and push that work.
Keep code with its tests; preserve other sessions' changes. A session can
produce several meaningful commits. Do not make empty commits for counts.

Denisse's code repository is public. Vault notes, conversation and personal
settings must not be added to it. The vault has its own private GitHub remote.
Existing committed vault history is backed up; uncommitted working files
require their own deliberate commits. A backup push never stages those files.

## Nightly checkpoints

The existing nightly job keeps its schedule and gains a sixth backup stage.
It checkpoints only its generated health report and task mirror, using the
vault's configured Git author. Automated origin is recorded in the commit
message. The previous `denisse@local` override is removed, so an account-linked
author email can receive normal GitHub attribution.

`commit_owned()` commits explicit literal paths with `git commit --only`,
leaving unrelated staged work untouched. No changes means no new commit.
Learning stores in `Denisse/Brain/` are no longer newly staged by this job.
Historical tracked files remain in the private vault's existing history.

Private backup is local, explicit opt-in:

```powershell
git -C "path-to-vault" config denisse.privateBackup true
```

The job uses the vault's `origin` push URL and GitHub CLI authentication.
Before each push it checks that the target is private and the current branch
is its default branch. The push uses the exact verified URL and captured
commit SHA. If privacy/authentication cannot be verified, a branch differs,
or a push fails, the job reports failure and keeps the local commits.
It never force-pushes, changes branches or resolves conflicts automatically.

Run `scripts/nightly.py --dry` to skip committing and all backup network calls.
Set `denisse.privateBackup` to `false` to disable backup without removing data.
Only committed history is uploaded; other staged and working files remain
under their owners' control. The Obsidian/OneDrive workflow is unchanged.

```powershell
.venv\Scripts\python.exe scripts\test_vault_git.py
```

These checks use temporary Git repositories and mocked GitHub responses.
They cover unrelated staged files, author identity, no-change checkpoints,
unsafe paths, opt-out, public/wrong targets, auth errors and branch mismatches.
