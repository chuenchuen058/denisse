"""Tests for orchestra/new_task.py: create() writes a queue task the runner reads back, from
any agent (body, gate and source filled in), never overwriting one. Temp folders only.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_new_task -v
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import new_task  # noqa: E402

TEMPLATE = (HERE.parent / ".review" / "TEMPLATE.md")
FALLBACK_TEMPLATE = """# <short title, what will be true when done>
Queue: draft
Priority: normal
Weight: light
Kind: work
Lane: claude
Written: 2026-10-31T16:00

<The facts the session needs: which files, what's wrong, what you want.
Write it so a session with no memory of today can do it.>

## Gate
<One line saying what "worked" means, then the commands.>

```
.venv\\Scripts\\python.exe scripts\\test_tasks.py
```
"""


class NewTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="new-task-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.review = self.tmp / ".review"
        self.review.mkdir()
        text = TEMPLATE.read_text(encoding="utf-8") if TEMPLATE.exists() else FALLBACK_TEMPLATE
        (self.review / "TEMPLATE.md").write_text(text, encoding="utf-8")

    def parse(self, path):
        os.environ.setdefault("ORCHESTRA_POWER", "ac")
        import runner
        return runner.parse_task(path)

    def test_create_from_an_agent_reads_back_as_a_runner_task(self):
        p = new_task.create("Summarise week 5 for the quiz", body="Read the week 5 notes.\nWrite a summary.",
                            gate=['.venv\\Scripts\\python.exe -c "pass"'], lane="codex", auto=True,
                            weight="heavy", source="agent hub queue_task", review=self.review)
        self.assertEqual(p.parent, self.review / "queue")
        text = p.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Summarise week 5 for the quiz\n"))
        self.assertIn("Read the week 5 notes.\nWrite a summary.", text)
        self.assertNotIn("<The facts", text, "the template's placeholder is replaced by the body")
        task = self.parse(p)
        self.assertEqual(task.title, "Summarise week 5 for the quiz")
        self.assertEqual(task.fields["queue"], "auto")
        self.assertEqual(task.fields["lane"], "codex")
        self.assertEqual(task.fields["weight"], "heavy")
        self.assertEqual(task.fields["from"], "agent hub queue_task")
        self.assertEqual(task.gate_commands, ['.venv\\Scripts\\python.exe -c "pass"'])

    def test_without_body_or_gate_the_template_stays_a_draft(self):
        p = new_task.create("Fix tray restart", lane="claude", review=self.review)
        text = p.read_text(encoding="utf-8")
        self.assertIn("Queue: draft", text)
        self.assertIn("<The facts", text)
        self.assertNotIn("From:", text)

    def test_never_overwrites_and_rejects_bad_values(self):
        a = new_task.create("Same", lane="claude", review=self.review)
        b = new_task.create("Same", lane="claude", review=self.review)
        self.assertNotEqual(a, b)
        self.assertTrue(a.exists() and b.exists())
        notes = []
        new_task.create("Same", lane="claude", review=self.review, say=notes.append)
        self.assertTrue(any("same title" in n for n in notes))
        for bad in ({"priority": "high"}, {"weight": "huge"}, {"kind": "chore"}):
            with self.assertRaises(ValueError):
                new_task.create("x", lane="claude", review=self.review, **bad)
        with self.assertRaises(ValueError):
            new_task.create("   ", lane="claude", review=self.review)

    def test_body_with_regex_characters_is_kept_as_written(self):
        body = r"Replace \1 and \g<0> in C:\Users\x; $5 a month."
        p = new_task.create("Odd body", body=body, lane="claude", review=self.review)
        self.assertIn(body, p.read_text(encoding="utf-8"))

    def test_cli_still_works(self):
        out = io.StringIO()
        with mock.patch.object(new_task, "REVIEW", self.review), redirect_stdout(out):
            body = self.tmp / "b.md"
            body.write_text("Do it.", encoding="utf-8")
            rc = new_task.main(["CLI task", "--lane", "claude", "--auto", "--body-file", str(body),
                                "--gate", "echo ok", "--from", "exam"])
        self.assertEqual(rc, 0)
        path = Path(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(path.parent, self.review / "queue")
        task = self.parse(path)
        self.assertEqual(task.fields["from"], "exam")
        self.assertEqual(task.gate_commands, ["echo ok"])


if __name__ == "__main__":
    unittest.main()
