"""Narrow vault checkpoints and opt-in, verified-private GitHub pushes."""
import json
import os
import re
import subprocess


class BackupError(RuntimeError):
    pass


def run(vault, *args, ok=(0,)):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    try:
        result = subprocess.run(args, cwd=vault, env=env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError(str(exc)) from exc
    if result.returncode not in ok:
        raise BackupError((result.stderr or result.stdout or "Command failed").strip())
    return result.stdout.strip()


def commit_owned(vault, paths, message):
    """Commit explicit literal paths, preserving unrelated staged changes."""
    if not paths:
        return None
    for path in paths:
        if (not path or path.startswith(("/", "\\", ":")) or ":" in path
                or ".." in path.replace("\\", "/").split("/")):
            raise BackupError("Checkpoint paths must be relative literal file names")
    specs = [":(literal)" + path for path in paths]
    if not run(vault, "git", "status", "--porcelain", "--", *specs):
        return None
    run(vault, "git", "add", "--", *specs)
    run(vault, "git", "commit", "--only", "-m", message, "--", *specs)
    return run(vault, "git", "rev-parse", "HEAD")


def push_private(vault):
    """Push committed history only, after explicit opt-in and privacy check.

    Bind the verified URL and commit SHA rather than rereading origin/HEAD
    during the push. Never force-push or change branches on failure.
    """
    enabled = run(vault, "git", "config", "--get", "--bool", "denisse.privateBackup", ok=(0, 1))
    if enabled != "true":
        return "disabled"
    url = run(vault, "git", "remote", "get-url", "--push", "origin")
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([\w-]+/[\w.-]+?)(?:\.git)?/?", url)
    if not match:
        raise BackupError("Backup origin must be a direct github.com repository URL")
    repo = match.group(1)
    try:
        metadata = json.loads(run(vault, "gh", "repo", "view", repo, "--json",
                                  "nameWithOwner,isPrivate,defaultBranchRef"))
    except (ValueError, TypeError) as exc:
        raise BackupError("Could not verify repository privacy") from exc
    if (not isinstance(metadata, dict) or metadata.get("isPrivate") is not True
            or str(metadata.get("nameWithOwner", "")).lower() != repo.lower()):
        raise BackupError("Backup refused: target is not the verified private repository")
    branch = run(vault, "git", "symbolic-ref", "--quiet", "--short", "HEAD")
    default = metadata.get("defaultBranchRef") or {}
    if not isinstance(default, dict) or default.get("name") != branch:
        raise BackupError("Backup refused: current branch is not the remote default branch")
    sha = run(vault, "git", "rev-parse", f"refs/heads/{branch}")
    run(vault, "git", "push", "--porcelain", "--", url, f"{sha}:refs/heads/{branch}")
    return "pushed"
