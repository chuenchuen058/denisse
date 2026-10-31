"""Tests for orchestra/runner.py with a fake lane (no model calls).

    .venv\\Scripts\\python.exe orchestra\\test_runner.py
Each test builds a throwaway git repo, so the real queue is never touched.
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_PROMPT = HERE / "review_prompt.md"

# Fake lane: writes the report the test asks for (via FAKE_RESULT / FAKE_HUMAN /
# FAKE_SLEEP / FAKE_COMMIT env vars) into the worktree, like a real session would.
FAKE = textwrap.dedent("""
    import os, sys, time, subprocess, pathlib
    sys.stdin.read()
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
    if os.environ.get("FAKE_COMMIT"):
        pathlib.Path("done.txt").write_text("x")
        subprocess.run(["git", "add", "done.txt"], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fake"], check=True)
    r = os.environ.get("FAKE_RESULT", "PASS")
    if r != "NONE":
        pathlib.Path(os.environ["ORCHESTRA_REPORT"]).write_text(
            f"RESULT: {r}\\nGATE: fake -> ok\\nHUMAN: {os.environ.get('FAKE_HUMAN', 'none')}\\n")
""")

TASK = """# {title}
Queue: {queue}
Priority: {priority}
Weight: light
Kind: {kind}
Lane: fake
Written: 2026-10-31T16:00

Do the thing.

## Gate
fake gate
"""


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        g("init", "-q", "-b", "main")
        g("config", "user.name", "t"); g("config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text(".review/*\n")
        (self.repo / "orchestra").mkdir()
        shutil.copy(SRC_PROMPT, self.repo / "orchestra" / "review_prompt.md")
        g("add", "."); g("commit", "-qm", "init")
        (self.repo / ".review").mkdir()
        (self.repo / ".review" / "lanes.json").write_text(json.dumps({"fake": [sys.executable, "-c", FAKE]}))
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_BACKOFF_MIN"] = "0"
        os.environ["ORCHESTRA_TIMEOUT_LIGHT"] = "3"
        for k in ("FAKE_RESULT", "FAKE_HUMAN", "FAKE_SLEEP", "FAKE_COMMIT"):
            os.environ.pop(k, None)
        sys.path.insert(0, str(HERE))
        import runner
        self.R = importlib.reload(runner)

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, slug, title="t", queue="auto", priority="normal", kind="work"):
        p = self.R.QUEUE / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TASK.format(title=title, queue=queue, priority=priority, kind=kind))
        return p

    def state(self):
        return json.loads(self.R.STATE_FILE.read_text())

    def test_pass_archives_and_merges(self):
        self.add("a")
        os.environ["FAKE_COMMIT"] = "1"
        self.assertEqual(self.R.tick(), "PASS")
        day = next((self.R.ARCHIVE).iterdir())
        self.assertTrue((day / "a.md").exists() and (day / "a.report.md").exists())
        self.assertTrue((self.repo / "done.txt").exists(), "branch should be merged")
        self.assertEqual(self.state()["tasks"]["a"]["merge"], "merged")
        self.assertFalse((self.R.WORKTREES / "a").exists())

    def test_retry_cap_parks_after_three(self):
        self.add("b")
        os.environ["FAKE_RESULT"] = "FAIL"
        results = [self.R.tick() for _ in range(4)]
        self.assertEqual(results[:3], ["FAIL"] * 3)
        self.assertEqual(results[3], "idle")
        self.assertTrue((self.R.PARKED / "b.md").exists())
        self.assertIn("retry cap", (self.R.PARKED / "b.why.txt").read_text())

    def test_human_line_parks_immediately(self):
        self.add("c")
        os.environ["FAKE_RESULT"] = "PARTIAL"
        os.environ["FAKE_HUMAN"] = "sign in to Outlook"
        self.R.tick()
        self.assertIn("waiting on Chuen", (self.R.PARKED / "c.why.txt").read_text())
        self.assertIn("sign in to Outlook", (self.R.REVIEW / "STATE_NOW.md").read_text())

    def test_timeout_kills_and_counts(self):
        self.add("d")
        os.environ["FAKE_SLEEP"] = "30"
        self.assertEqual(self.R.tick(), "TIMEOUT")
        st = self.state()["tasks"]["d"]
        self.assertEqual(st["attempts"], 1)
        self.assertLess(st["history"][0]["seconds"], 15)

    def test_no_report_is_fail(self):
        self.add("e")
        os.environ["FAKE_RESULT"] = "NONE"
        self.assertEqual(self.R.tick(), "FAIL")

    def test_urgent_and_work_first(self):
        self.add("1-maint", kind="maintenance")
        self.add("2-work")
        self.add("3-urgent", priority="urgent")
        st = self.R.load_state()
        self.assertEqual(self.R.pick(self.R.load_queue(), st).slug, "3-urgent")
        (self.R.QUEUE / "3-urgent.md").unlink()
        self.assertEqual(self.R.pick(self.R.load_queue(), st).slug, "2-work")

    def test_maintenance_cap(self):
        self.add("m", kind="maintenance")
        st = self.R.load_state()
        u = self.R.today_usage(st)
        u.update(work_s=3600, maint_s=1800)  # 33% already
        self.assertIsNone(self.R.pick(self.R.load_queue(), st))
        u.update(work_s=36000, maint_s=1800)  # 5%
        self.assertEqual(self.R.pick(self.R.load_queue(), st).slug, "m")

    def test_not_auto_or_placeholder_is_skipped(self):
        self.add("f", queue="draft")
        p = self.add("g")
        p.write_text(p.read_text().replace("fake gate", "<Exactly how to tell>"))
        self.assertEqual(self.R.tick(), "idle")

    def test_busy_lock(self):
        self.add("h")
        self.R.LOCK_FILE.write_text("{}")
        self.assertEqual(self.R.tick(), "busy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
