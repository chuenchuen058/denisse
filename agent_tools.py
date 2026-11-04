"""Tools bound to standing instructions. No arbitrary shell or file arguments.

Limits and wording are data: the "agent_tools" section of housekeeping.json
(AGENTS.md Rule 0).
"""
import os
from pathlib import Path
import subprocess

import housekeeping

CONFIG = {}


def configure(path=None) -> dict:
    """(Re)load housekeeping.json's "agent_tools" section."""
    global CONFIG
    CONFIG = housekeeping.load(path)["agent_tools"]
    return CONFIG


def _text(key, **fields) -> str:
    return CONFIG["text"][key].format(**fields)


configure()


def task_snapshot():
    # list_open_tasks() also imports vault ticks, so it is NOT read-only.
    import tasks
    if Path(tasks.TASKS_FILE).exists():
        import json
        with open(tasks.TASKS_FILE, encoding="utf-8") as source:
            records = json.load(source)
        if not isinstance(records, list):
            raise ValueError(_text("store_not_list"))
    else:
        records = []
    opened = [t for t in records if not t.get("done_at")]
    opened.sort(key=lambda t: (t.get("due") is None, t.get("due") or ""))
    limit, chars = CONFIG["task_limit"], CONFIG["task_text_chars"]
    return {"open_count": len(opened), "shown": min(limit, len(opened)),
            "tasks": [{"id": t["id"], "text": str(t["text"])[:chars],
                       "due": t.get("due"), "priority": t.get("priority", CONFIG["default_priority"])}
                      for t in opened[:limit]]}


def project_snapshot(project):
    root = Path(project).resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise ValueError(_text("not_a_repo"))

    def git(*args, ok=(0,)):
        response = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=CONFIG["git_timeout_s"],
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        if response.returncode not in ok:
            raise RuntimeError((response.stderr or _text("git_failed")).strip()[:CONFIG["git_error_chars"]])
        return response.stdout.strip()

    return {"project": root.name,
            "branch": git("branch", "--show-current"),
            "changes": git("status", "--porcelain=v1", "--no-renames")[:CONFIG["changes_chars"]],
            "latest_commit": git("log", "-1", "--format=%h %s", ok=(0, 128))[:CONFIG["commit_chars"]],
            "limits": _text("project_limits")}


def organize_downloads():
    import auto_organize
    moved = []
    for folder in auto_organize.WATCH_FOLDERS:
        if not Path(folder).is_dir():
            raise FileNotFoundError(_text("downloads_missing", folder=folder))
        moved.extend(auto_organize.organize_folder(folder))
    return {"moved": len(moved), "moves": moved,
            "limits": _text("downloads_limits")}


def render_brief(job_id, result):
    """Observed facts first. A model suggestion never serves as action evidence."""
    if result["status"] != "completed":
        return _text("not_completed", job_id=job_id, status=result["status"], error=result.get("error", ""))
    evidence = result["evidence"]
    if job_id == "task_review":
        snapshot = evidence["task_snapshot"]
        lines = [_text("tasks_header", count=snapshot.get("open_count", len(snapshot["tasks"])))]
        focus = set(result["focus_ids"])
        chosen = ([t for t in snapshot["tasks"] if t["id"] in focus]
                  or snapshot["tasks"][:CONFIG["brief_fallback_tasks"]])
        lines += [_text("task_line", text=t["text"], due=t.get("due") or _text("due_unset")) for t in chosen]
        lines += [_text("tasks_next") if chosen else _text("no_tasks")]
    elif job_id == "project_review":
        snapshot = evidence["project_snapshot"]
        lines = [_text("project_header", project=snapshot["project"], branch=snapshot["branch"] or _text("no_branch")),
                 snapshot["latest_commit"] or _text("no_commits"),
                 snapshot["changes"] or _text("clean_tree"), snapshot["limits"]]
        lines += [_text("project_next_dirty") if snapshot["changes"] else _text("project_next_clean")]
    else:
        return _text("downloads", moved=evidence["organize_downloads"]["moved"])
    return "\n".join(lines)
