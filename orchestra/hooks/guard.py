"""PreToolUse hook for Orchestra sessions, loaded by the runner with `claude -p --settings`.

Real limits live here, not in the allow list (Thomas, 31 Oct: `Bash(python:*)` and
`Bash(git:*)` already allow pushing, `git -C ../denisse commit`, `reset --hard`...).

  * Write/Edit outside this session's worktree -> denied, with the worktree path to use
  * Bash: git push, git -C, reset --hard, clean -f, gh, recursive deletes, or any
    command naming the main checkout -> denied
Every call appends to <worktree>/.review/hook.log, so the runner can tell the hook loaded.
Fails open on its own errors (a broken guard must not stall a run), and that is logged too.
This is a guard rail, not a sandbox: `python -c` can still do anything Python can.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
BASH_DENY = [
    (r"\bgit\b[^|;&]*\bpush\b", "pushing is Chuen's step"),
    (r"\bgit\s+-C\b", "git -C can reach another checkout; run git inside this worktree"),
    (r"\breset\s+--hard\b", "reset --hard throws work away"),
    (r"\bclean\s+-\w*f", "git clean -f deletes untracked files"),
    (r"(^|[\s;&|])gh\s", "the GitHub CLI acts on the public repo"),
    (r"\brm\s+-\w*r\w*f|\brm\s+-\w*f\w*r|\bRemove-Item\b[^|;&]*-Recurse|\brmdir\s+/s", "recursive delete"),
    (r"\bworktree\s+(remove|prune)\b", "the runner manages worktrees"),
]


def norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)).replace("\\", "/").rstrip("/")


def decide(data: dict, worktree: str, main: str) -> str | None:
    tool = data.get("tool_name")
    inp = data.get("tool_input") or {}
    if tool in WRITE_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if not path:
            return None
        cwd = data.get("cwd") or worktree
        target = norm(path if os.path.isabs(path) else os.path.join(cwd, path))
        wt = norm(worktree)
        if target == wt or target.startswith(wt + "/"):
            return None
        hint = ""
        m = norm(main)
        if target.startswith(m + "/"):
            hint = f" Write {os.path.join(worktree, target[len(m) + 1:])} instead."
        return f"Writes must stay inside this task's worktree {worktree}; {path} is outside it.{hint}"
    if tool == "Bash":
        cmd = inp.get("command") or ""
        for pat, why in BASH_DENY:
            if re.search(pat, cmd, re.I):
                return f"Denied by the Orchestra guard ({why}). Finish the rest and note this under ODD."
        low = cmd.replace("\\", "/").lower()
        mains = {norm(main).lower(), str(main).replace("\\", "/").lower().rstrip("/")}
        if any(m and m in low.replace(m + "-wt", "") for m in mains):
            return f"Denied: this command names the main checkout {main}. Work in the worktree {worktree}."
    return None


def main() -> int:
    worktree = os.environ.get("ORCHESTRA_WORKTREE", "")
    mainrepo = os.environ.get("ORCHESTRA_MAIN", "")
    log = Path(worktree, ".review", "hook.log") if worktree else None
    try:
        data = json.load(sys.stdin)
        why = decide(data, worktree, mainrepo) if worktree else None
        if log:
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{data.get('tool_name')} {'DENY ' + why if why else 'ok'}\n")
        if why:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "permissionDecision": "deny",
                                                     "permissionDecisionReason": why}}))
    except Exception as e:  # fail open, but leave a trace
        if log:
            with log.open("a", encoding="utf-8") as f:
                f.write(f"guard error: {e!r}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
