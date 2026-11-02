"""Tests for aftercare.py: the vault note and the push's privacy check. Temp dirs only."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aftercare  # noqa: E402

REPORT = """RESULT: PASS
GATE: tests -> OK
HUMAN: none
PREMISE: right
DONE:
- changed a.py
NOT DONE:
- none
ODD:
- b.py looked unused
COMMITS: abc123
"""
TASK = "# Fix the thing\nQueue: auto\nLane: codex\n\nMake a.py do X.\nKeep b.py.\n\n## Gate\n```\nx\n```\n"


def g(repo, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo,
                          check=True, capture_output=True, text=True)


class TestAftercare(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_vault_note_appends_and_commits(self):
        v = self.tmp / "vault"
        v.mkdir()
        g(v, "init", "-q")
        for i in (1, 2):
            note = aftercare.vault_note(v, slug=f"s{i}", title="Fix the thing", result="PASS", lane="codex",
                                        seconds=600, spent="$0.00", merge="merged", pushed="pushed 1 commit(s)",
                                        task_text=TASK, report_text=REPORT, report_path="r.md")
        t = note.read_text(encoding="utf-8")
        self.assertTrue(t.startswith("---\nname: Orchestra "))
        self.assertEqual(t.count("## "), 2)
        self.assertIn("**PASS, merged, pushed 1 commit(s)** · lane codex · 10 min", t)
        self.assertIn("Asked: Make a.py do X. Keep b.py.", t)
        self.assertIn("- changed a.py", t)
        self.assertNotIn("Not done", t)  # "none" is left out
        self.assertIn("b.py looked unused", t)
        log = subprocess.run(["git", "log", "--oneline"], cwd=v, capture_output=True, text=True).stdout
        self.assertEqual(len(log.splitlines()), 2)

    def test_privacy_check(self):
        r = self.tmp / "repo"
        r.mkdir()
        g(r, "init", "-q")
        (r / "a.py").write_text("x = 1\n")
        g(r, "add", "a.py")
        g(r, "commit", "-qm", "base")
        g(r, "branch", "up")
        self.assertEqual(aftercare.privacy_problems(r, "up"), [])
        (r / ".env.example").write_text("KEY=\n")
        (r / "memory").mkdir()
        (r / "memory" / ".gitkeep").write_text("")
        g(r, "add", ".")
        g(r, "commit", "-qm", "ok")
        self.assertEqual(aftercare.privacy_problems(r, "up"), [])
        (r / "Atlas").mkdir()
        (r / "Atlas" / "Settled.md").write_text("private\n")
        (r / "b.py").write_text("K = 'sk-" + "a" * 30 + "'\n")
        g(r, "add", ".")
        g(r, "commit", "-qm", "bad")
        p = " ".join(aftercare.privacy_problems(r, "up"))
        self.assertIn("Atlas/Settled.md", p)
        self.assertIn("key or token", p)


class TestParallelState(unittest.TestCase):
    def test_two_lanes_merge_their_changes(self):
        import runner
        base = {"tasks": {"a": {"attempts": 0}}, "done": {}, "durations": {"light": [5], "heavy": []},
                "days": {"d": {"work_s": 100, "cost_usd": 1.0}}}
        other = {"tasks": {"a": {"attempts": 0}, "b": {"attempts": 1}}, "done": {"hb": "t"},
                 "durations": {"light": [5, 7], "heavy": []}, "days": {"d": {"work_s": 160, "cost_usd": 1.5}}}
        mine = {"tasks": {"a": {"attempts": 1}}, "done": {"ha": "t"},
                "durations": {"light": [5], "heavy": [900]}, "days": {"d": {"work_s": 400, "cost_usd": 3.0}}}
        out = runner._merge_into(other, mine, base)
        self.assertEqual(out["tasks"], {"a": {"attempts": 1}, "b": {"attempts": 1}})
        self.assertEqual(out["done"], {"hb": "t", "ha": "t"})
        self.assertEqual(out["durations"], {"light": [5, 7], "heavy": [900]})
        self.assertEqual(out["days"]["d"], {"work_s": 460, "cost_usd": 3.5})


if __name__ == "__main__":
    unittest.main(verbosity=2)
