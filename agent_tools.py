"""Tools bound to standing instructions. No arbitrary shell or file arguments."""
import os
from pathlib import Path
import subprocess


def task_snapshot():
    # list_open_tasks() also imports vault ticks, so it is NOT read-only.
    import tasks
    if Path(tasks.TASKS_FILE).exists():
        import json
        with open(tasks.TASKS_FILE, encoding="utf-8") as source:
            records = json.load(source)
        if not isinstance(records, list):
            raise ValueError("Task store is not a list")
    else:
        records = []
    opened = [t for t in records if not t.get("done_at")]
    opened.sort(key=lambda t: (t.get("due") is None, t.get("due") or ""))
    return {"open_count": len(opened), "shown": min(20, len(opened)),
            "tasks": [{"id": t["id"], "text": str(t["text"])[:240],
                       "due": t.get("due"), "priority": t.get("priority", "medium")}
                      for t in opened[:20]]}


def project_snapshot(project):
    root = Path(project).resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise ValueError("Configured project is not a Git repository root")

    def git(*args, ok=(0,)):
        response = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        if response.returncode not in ok:
            raise RuntimeError((response.stderr or "Git observation failed").strip()[:400])
        return response.stdout.strip()

    return {"project": root.name,
            "branch": git("branch", "--show-current"),
            "changes": git("status", "--porcelain=v1", "--no-renames")[:6000],
            "latest_commit": git("log", "-1", "--format=%h %s", ok=(0, 128))[:500],
            "limits": "Local Git state only; no build, tests, fetch or code changes performed."}


def organize_downloads():
    import auto_organize
    moved = []
    for folder in auto_organize.WATCH_FOLDERS:
        if not Path(folder).is_dir():
            raise FileNotFoundError(f"Downloads folder unavailable: {folder}")
        moved.extend(auto_organize.organize_folder(folder))
    return {"moved": len(moved), "moves": moved,
            "limits": "Existing deterministic rules; folders are never selected by the model."}


def render_brief(job_id, result):
    """Observed facts first. A model suggestion never serves as action evidence."""
    if result["status"] != "completed":
        return f"{job_id}: {result['status']}. {result.get('error', '')}"
    evidence = result["evidence"]
    if job_id == "task_review":
        snapshot = evidence["task_snapshot"]
        lines = [f"{snapshot.get('open_count', len(snapshot['tasks']))} open tasks (review only)."]
        focus = set(result["focus_ids"])
        chosen = [t for t in snapshot["tasks"] if t["id"] in focus] or snapshot["tasks"][:3]
        lines += [f"- {t['text']} | due: {t.get('due') or 'not set'}" for t in chosen]
    elif job_id == "project_review":
        snapshot = evidence["project_snapshot"]
        lines = [f"{snapshot['project']} ({snapshot['branch'] or 'detached/unborn'}).",
                 snapshot["latest_commit"] or "No commits yet.",
                 snapshot["changes"] or "Working tree clean.", snapshot["limits"]]
    else:
        return f"Downloads: {evidence['organize_downloads']['moved']} files organised."
    if result["suggestion"]:
        lines += ["Suggested next step (model): " + result["suggestion"]]
    return "\n".join(lines)
