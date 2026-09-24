"""Regression test for the two-way Obsidian task mirror.

Writing `- [ ]` checkboxes into a vault and ignoring them would be a trap:
Obsidian renders real checkboxes, so a tick has to count. This covers the
round trip and the two ways it could go wrong -- re-closing everything
because the Done section is full of [x], and losing the id-to-task link.

Uses a temp vault and a temp task store; touches nothing real.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import obsidian_sync
import tasks

tmp = Path(tempfile.mkdtemp())
tasks.TASKS_FILE = str(tmp / "tasks.json")
obsidian_sync.DENISSE_DIR = str(tmp / "vault")
obsidian_sync.VAULT_DIR = str(tmp / "vault")
Path(obsidian_sync.DENISSE_DIR).mkdir(parents=True, exist_ok=True)

import obsidian_tasks
obsidian_tasks.TASKS_NOTE = str(Path(obsidian_sync.DENISSE_DIR) / "Tasks.md")
obsidian_tasks.ARCHIVE_DIR = str(Path(obsidian_sync.DENISSE_DIR) / "Task Archive")

fails = []


def check(name, ok):
    print(f"[{'OK ' if ok else 'FAIL'}] {name}")
    if not ok:
        fails.append(name)


a = tasks.add_task("sign the UKEC msoc contract", "friday")
b = tasks.add_task("order the N20 motor", "")
note = Path(obsidian_tasks.TASKS_NOTE)

check("note written on save", note.exists())
text = note.read_text(encoding="utf-8")
check("both tasks appear as unticked boxes", text.count("- [ ] ") == 2)
check("task id embedded as a block ref", f"^{a['id']}" in text)

# Chuen ticks one box in Obsidian.
note.write_text(
    text.replace(f"- [ ] {tasks.describe(a)} ^{a['id']}",
                 f"- [x] {tasks.describe(a)} ^{a['id']}"),
    encoding="utf-8",
)
closed = tasks.pull_vault_ticks()
check("tick closes exactly that task",
      [t["id"] for t in closed] == [a["id"]])
check("it is done in the store",
      [t["text"] for t in tasks.list_tasks()] == ["order the N20 motor"])

# The Done section is full of [x] by construction. Reading ticks again must
# not re-close everything -- pull_ticked only reads the Open section.
again = tasks.pull_vault_ticks()
check("second pull closes nothing", again == [])
check("the other task is still open", len(tasks.list_tasks()) == 1)

text = note.read_text(encoding="utf-8")
check("done task moved to the Done section",
      "## Done in the last 14 days (1)" in text)
check("open count updated", "## Open (1)" in text)

archive = Path(obsidian_tasks.ARCHIVE_DIR)
months = list(archive.glob("*.md")) if archive.exists() else []
check("month archive written", len(months) == 1)
if months:
    arch = months[0].read_text(encoding="utf-8")
    check("archive names the completed task", "UKEC msoc contract" in arch)

# A vault that isn't there must not break saving.
obsidian_sync.DENISSE_DIR = "Z:\\nope\\missing"
obsidian_tasks.TASKS_NOTE = "Z:\\nope\\missing\\Tasks.md"
try:
    tasks.add_task("still works without a vault", "")
    check("task still saves when the vault is unreachable",
          len(tasks.list_tasks()) == 2)
except Exception as exc:
    check(f"task still saves when the vault is unreachable ({exc})", False)

print("\n" + ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
