"""PreToolUse hook for Orchestra sessions, loaded by the runner with `claude -p --settings`.

Real limits live here, not in the allow list (Thomas, 31 Oct: `Bash(python:*)` and
`Bash(git:*)` already allow pushing, `git -C ../denisse commit`, `reset --hard`...).

  * Write/Edit outside this session's worktree -> denied, with the worktree path to use.
    Paths are resolved with realpath first: the worktree's .venv is a junction to the main
    checkout's venv, so <worktree>/.venv/... looks inside but lands in the main checkout.
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

GIT = r"(^|[;&|(\n])\s*git\s+(?:-c\s+\S+\s+|--?[\w-]+(?:=\S+)?\s+)*"
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
BASH_DENY = [
    # git subcommands only count at a command position, so the word "push" inside a
    # heredoc or commit message isn't a push (first real run, 31 Oct: a report was refused)
    (GIT + r"push\b", "pushing is Chuen's step"),
    (r"(^|[;&|(\n])\s*git\s+-C\b", "git -C can reach another checkout; run git inside this worktree"),
    (GIT + r"reset\s+--hard\b", "reset --hard throws work away"),
    (GIT + r"clean\s+-\w*f", "git clean -f deletes untracked files"),
    (r"(^|[\s;&|])gh\s", "the GitHub CLI acts on the public repo"),
    (r"\brm\s+-\w*r\w*f|\brm\s+-\w*f\w*r|\bRemove-Item\b[^|;&]*-Recurse|\brmdir\s+/s", "recursive delete"),
    (r"\bworktree\s+(remove|prune)\b", "the runner manages worktrees"),
]


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)


def strip_heredocs(cmd: str) -> str:
    """A heredoc body is text being written, not commands being run."""
    return HEREDOC.sub("<<HEREDOC", cmd)


def norm(p: str, real: bool = False) -> str:
    """Compare-ready path: absolute, case-folded, forward slashes. real=True also follows
    symlinks and junctions, so the check is on where a write actually lands."""
    p = os.path.realpath(p) if real else os.path.abspath(p)
    return os.path.normcase(p).replace("\\", "/").rstrip("/")


def inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def decide(data: dict, worktree: str, main: str) -> str | None:
    tool = data.get("tool_name")
    inp = data.get("tool_input") or {}
    if tool in WRITE_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if not path:
            return None
        cwd = data.get("cwd") or worktree
        full = path if os.path.isabs(path) else os.path.join(cwd, path)
        target, wt = norm(full, real=True), norm(worktree, real=True)
        if inside(target, wt):
            return None
        if inside(norm(full), norm(worktree)):
            # looks inside, resolves outside: a link such as the .venv junction
            return (f"Writes must stay inside this task's worktree {worktree}; {path} goes through "
                    f"a link and really lands in {os.path.realpath(full)}. Don't write through links.")
        hint = ""
        m = norm(main, real=True)
        if inside(target, m):
            hint = f" Write {os.path.join(worktree, target[len(m) + 1:])} instead."
        return f"Writes must stay inside this task's worktree {worktree}; {path} is outside it.{hint}"
    if tool == "Bash":
        cmd = strip_heredocs(inp.get("command") or "")
        for pat, why in BASH_DENY:
            if re.search(pat, cmd, re.I | re.M):
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
