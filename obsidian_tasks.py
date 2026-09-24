"""
Mirror Denisse's task list into the Obsidian vault, and read ticks back.

Two-way on purpose. Writing `- [ ]` checkboxes into a vault and then
ignoring them would be a trap: Obsidian renders them as real checkboxes, so
Chuen will tick one and reasonably expect it to count. `pull_ticked()` reads
those ticks back and closes the tasks, so the note is a working surface
rather than a read-only mirror.

Each line carries the task id as an Obsidian block ref (`^ab12cd34`) --
unobtrusive in the rendered note, stable across edits to the text, and it
means matching a ticked line back to a task never relies on the wording.

Layout in the vault:
    Denisse/Tasks.md              open tasks + recently done, tickable
    Denisse/Task Archive/YYYY-MM.md   everything completed that month
"""

import os
import re
from datetime import datetime, timedelta

import obsidian_sync

TASKS_NOTE = os.path.join(obsidian_sync.DENISSE_DIR, "Tasks.md")
ARCHIVE_DIR = os.path.join(obsidian_sync.DENISSE_DIR, "Task Archive")

_TICKED = re.compile(r"^\s*-\s*\[[xX]\]\s.*?\^([0-9a-f]{8})\s*$", re.M)
_UNTICKED = re.compile(r"^\s*-\s*\[\s\]\s.*?\^([0-9a-f]{8})\s*$", re.M)


def _line(t, tasks_mod, done=False) -> str:
    box = "[x]" if done else "[ ]"
    return f"- {box} {tasks_mod.describe(t)} ^{t['id']}"


def sync(tasks_mod) -> bool:
    """Rewrite Tasks.md and this month's archive from the live task list.

    Best-effort like everything else that touches the vault: an unsynced
    OneDrive path or a locked file must never break a chat turn.
    """
    if not obsidian_sync._vault_available():
        return False
    try:
        all_tasks = tasks_mod.list_tasks(include_done=True)
        open_tasks = [t for t in all_tasks if not t["done_at"]]
        open_tasks.sort(key=lambda t: (t["due"] is None, t["due"] or ""))
        done = [t for t in all_tasks if t["done_at"]]
        done.sort(key=lambda t: t["done_at"], reverse=True)

        cutoff = (datetime.now() - timedelta(days=14)).isoformat(timespec="minutes")
        recent = [t for t in done if t["done_at"] >= cutoff]

        lines = [
            "---",
            "name: Tasks",
            'description: "Denisse\'s live task list -- tick a box here and she picks it up"',
            f"updated: {datetime.now().date().isoformat()}",
            "tags: [denisse, tasks]",
            "---",
            "",
            "# Tasks",
            "",
            "Written by Denisse. **Ticking a box here marks it done** the next time "
            "she reads the list -- you don't have to tell her separately. Editing the "
            "text of a line won't rename the task; say so in chat instead.",
            "",
            f"## Open ({len(open_tasks)})",
            "",
        ]
        lines += [_line(t, tasks_mod) for t in open_tasks] or ["_Nothing outstanding._"]
        lines += ["", f"## Done in the last 14 days ({len(recent)})", ""]
        lines += [_line(t, tasks_mod, done=True) for t in recent] or ["_Nothing yet._"]
        lines += ["", "Older completed tasks are in `Task Archive/`.", ""]

        os.makedirs(obsidian_sync.DENISSE_DIR, exist_ok=True)
        with open(TASKS_NOTE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        _write_archive(done, tasks_mod)
        return True
    except OSError:
        return False


def _write_archive(done: list, tasks_mod) -> None:
    """One note per calendar month of completed tasks. Rewritten from the
    live data each time rather than appended to, so re-running is safe."""
    if not done:
        return
    months = {}
    for t in done:
        months.setdefault(t["done_at"][:7], []).append(t)

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    for month, items in months.items():
        items.sort(key=lambda t: t["done_at"])
        lines = [
            "---",
            f"name: {month}",
            f'description: "Tasks Chuen completed in {month}"',
            f"updated: {datetime.now().date().isoformat()}",
            "tags: [denisse, tasks, archive]",
            "---",
            "",
            f"# Completed — {month}",
            "",
            f"{len(items)} task{'s' if len(items) != 1 else ''}.",
            "",
        ]
        for t in items:
            when = datetime.fromisoformat(t["done_at"])
            lines.append(f"- **{when:%a %d %b %H:%M}** — {t['text']} ^{t['id']}")
        lines.append("")
        with open(os.path.join(ARCHIVE_DIR, f"{month}.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))


def pull_ticked() -> list:
    """Task ids ticked in Tasks.md. Returns [] on any problem -- a vault
    that's unreadable must never stop the task list working."""
    if not os.path.exists(TASKS_NOTE):
        return []
    try:
        with open(TASKS_NOTE, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    # Only the Open section matters; the Done section is full of [x] by
    # construction and would otherwise re-close everything every sync.
    head, _, _ = text.partition("## Done in the last")
    return _TICKED.findall(head)
