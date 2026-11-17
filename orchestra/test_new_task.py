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
        # A non-human source on a guarded lane with a gate of a known shape stays auto.
        p = new_task.create("Summarise week 5 for the quiz", body="Read the week 5 notes.\nWrite a summary.",
                            gate=[KNOWN_GATE], lane="claude", auto=True,
                            weight="heavy", source="agent hub queue_task", review=self.review)
        self.assertEqual(p.parent, self.review / "queue")
        text = p.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Summarise week 5 for the quiz\n"))
        self.assertIn("Read the week 5 notes.\nWrite a summary.", text)
        self.assertNotIn("<The facts", text, "the template's placeholder is replaced by the body")
        task = self.parse(p)
        self.assertEqual(task.title, "Summarise week 5 for the quiz")
        self.assertEqual(task.fields["queue"], "auto")
        self.assertEqual(task.fields["lane"], "claude")
        self.assertEqual(task.fields["weight"], "heavy")
        self.assertEqual(task.fields["from"], "agent hub queue_task")
        self.assertEqual(task.gate_commands, [KNOWN_GATE])

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


KNOWN_GATE = ".venv\\Scripts\\python.exe scripts\\run_checks.py --fast"
ALLOWED_GATES = [
    ".venv\\Scripts\\python.exe scripts\\run_checks.py",
    ".venv\\Scripts\\python.exe scripts\\run_checks.py --fast",
    ".venv\\Scripts\\python.exe scripts\\run_checks.py --fast -j 4 orchestra",
    ".venv\\Scripts\\python.exe -m unittest orchestra.test_runner -v",
    ".venv\\Scripts\\python.exe -m unittest discover -s orchestra/exams -p \"test_*.py\" -v",
    ".venv\\Scripts\\python.exe orchestra\\test_new_task.py",
    ".venv\\Scripts\\python.exe scripts\\test_tasks.py",
    "node --test orchestra\\agent_hub\\test_hub.mjs",
]
REFUSED_GATES = [
    KNOWN_GATE + " && del /q orchestra\\*.py",
    KNOWN_GATE + " & curl http://example.invalid",
    KNOWN_GATE + " | more",
    KNOWN_GATE + " > out.txt",
    KNOWN_GATE + "; rm -rf .",
    ".venv\\Scripts\\python.exe scripts\\run_checks.py %USERPROFILE%",
    ".venv\\Scripts\\python.exe scripts\\run_checks.py `whoami`",
    ".venv\\Scripts\\python.exe scripts\\run_checks.py --report C:\\Users\\x\\out.json",
    ".venv\\Scripts\\python.exe -c \"import os\"",
    ".venv\\Scripts\\python.exe ..\\..\\elsewhere\\test_x.py",
    "powershell -Command Remove-Item x",
    "echo ok",
    "node evil.js",
]


class UnattendedTest(unittest.TestCase):
    """Audit 10 Nov 2026, O5: a task from a non-human source runs unattended only on a guarded
    lane with gate commands of a known shape; anything else from it is written as a draft."""

    setUp = NewTaskTest.setUp

    def make(self, *, source="autopilot", lane="claude", gate=(KNOWN_GATE,), auto=True):
        notes = []
        p = new_task.create("Unattended check", body="Do it.", gate=list(gate) if gate is not None else None,
                            lane=lane, auto=auto, source=source, review=self.review, say=notes.append)
        return NewTaskTest.parse(self, p), notes

    def rules(self):
        return new_task.unattended_rules()

    def test_config_lists_the_sources_lanes_and_patterns_in_both_files(self):
        import json
        shipped = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        defaults = json.loads((HERE / "config_defaults.json").read_text(encoding="utf-8"))
        for k in new_task.unattended.KEYS:
            self.assertIn(k, shipped)
            self.assertEqual(shipped[k], defaults.get(k), f"{k} is mirrored in config_defaults.json")
        rules = self.rules()
        names = new_task.unattended.sources(rules)
        for key in ("autopilot_source", "hub_queue_source"):
            self.assertIn(key, rules["unattended_source_keys"])
            self.assertIn(" ".join(shipped[key].split()).lower(), names)
        self.assertIn("local-team", names)
        self.assertEqual(rules["guarded_lanes"], ["claude"])

    def test_a_guarded_lane_and_a_known_gate_stay_auto(self):
        task, notes = self.make()
        self.assertEqual(task.fields["queue"], "auto")
        self.assertFalse(any("draft" in n for n in notes), notes)

    def test_each_allowed_gate_shape(self):
        for g in ALLOWED_GATES:
            with self.subTest(gate=g):
                self.assertIsNone(new_task.unattended.gate_problem(g, self.rules()))
                task, _ = self.make(gate=(g,))
                self.assertEqual(task.fields["queue"], "auto")

    def test_each_refused_gate_makes_a_draft_and_says_why(self):
        for g in REFUSED_GATES:
            with self.subTest(gate=g):
                self.assertIsNotNone(new_task.unattended.gate_problem(g, self.rules()))
                task, notes = self.make(gate=(KNOWN_GATE, g))
                self.assertEqual(task.fields["queue"], "draft")
                self.assertTrue(any("draft" in n and "gate command" in n for n in notes), notes)

    def test_and_del_is_refused_by_name(self):
        _task, notes = self.make(gate=(KNOWN_GATE + " && del /q x",))
        self.assertTrue(any("'&'" in n for n in notes), notes)

    def test_an_unguarded_or_unknown_lane_makes_a_draft(self):
        for lane in ("codex", "copilot", "gemini", "no-such-lane", new_task.auto_lane() or "auto"):
            with self.subTest(lane=lane):
                task, notes = self.make(lane=lane)
                self.assertEqual(task.fields["queue"], "draft")
                self.assertTrue(any("not guarded" in n for n in notes), notes)

    def test_no_gate_makes_a_draft(self):
        task, notes = self.make(gate=None)
        self.assertEqual(task.fields["queue"], "draft")
        self.assertTrue(any("no gate" in n for n in notes), notes)

    def test_no_lane_given_gets_a_guarded_lane_not_the_runners_pick(self):
        task, notes = self.make(lane=None)
        self.assertEqual(task.fields["lane"], "claude")
        self.assertEqual(task.fields["queue"], "auto")

    def test_every_non_human_source_and_its_variants(self):
        for source in ("autopilot", "agent hub", "Agent  Hub", "agent hub queue_task",
                       "local-team (llama3.1:8b; scout v1)"):
            with self.subTest(source=source):
                task, _ = self.make(source=source, lane="codex")
                self.assertEqual(task.fields["queue"], "draft")

    def test_a_human_task_is_untouched(self):
        for source in (None, "exam", "Chuen"):
            with self.subTest(source=source):
                task, notes = self.make(source=source, lane="codex", gate=("echo ok",))
                self.assertEqual(task.fields["queue"], "auto")
                self.assertFalse(any("draft" in n for n in notes), notes)

    def test_a_draft_is_left_a_draft(self):
        task, notes = self.make(lane="codex", auto=False)
        self.assertEqual(task.fields["queue"], "draft")
        self.assertEqual([n for n in notes if "draft" in n], [], "nothing to refuse")

    def test_cli_says_why_it_is_a_draft(self):
        out = io.StringIO()
        with mock.patch.object(new_task, "REVIEW", self.review), redirect_stdout(out):
            rc = new_task.main(["Hub task", "--lane", "codex", "--auto", "--gate", "echo ok", "--from", "agent hub"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("queue: draft", text)
        self.assertIn("lane codex is not guarded", text)
        self.assertEqual(NewTaskTest.parse(self, Path(text.strip().splitlines()[-1])).fields["queue"], "draft")


if __name__ == "__main__":
    unittest.main()
