"""Tests for orchestra/runner.py and hooks/guard.py with a fake lane (no model calls).

    .venv\\Scripts\\python.exe orchestra\\test_runner.py
Each test builds a throwaway git repo, so the real queue is never touched.
"""
import datetime as dt
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
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "hooks"))
import guard  # noqa: E402

# Fake lane, steered by env vars: FAKE_RESULT (PASS/PARTIAL/FAIL/NONE), FAKE_HUMAN,
# FAKE_SLEEP, FAKE_COMMIT, FAKE_STRAY (a path to write outside the worktree), FAKE_JUNK.
FAKE = textwrap.dedent("""
    import os, sys, time, subprocess, pathlib, json
    sys.stdin.read()
    if os.environ.get("FAKE_JUNK_CHECK") and pathlib.Path("junk.txt").exists():
        pathlib.Path(os.environ["FAKE_JUNK_CHECK"]).write_text("dirty worktree reused")
    if os.environ.get("FAKE_JUNK"):
        pathlib.Path("junk.txt").write_text("leftover")
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
    if os.environ.get("FAKE_COMMIT"):
        pathlib.Path("done.txt").write_text("x")
        subprocess.run(["git", "add", "done.txt"], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fake"], check=True)
    if os.environ.get("FAKE_STRAY"):
        pathlib.Path(os.environ["FAKE_STRAY"]).write_text("oops")
    r = os.environ.get("FAKE_RESULT", "PASS")
    if r != "NONE":
        pathlib.Path(os.environ["ORCHESTRA_REPORT"]).write_text(
            f"RESULT: {r}\\nGATE: fake -> ok\\nHUMAN: {os.environ.get('FAKE_HUMAN', 'none')}\\n"
            f"PREMISE: {os.environ.get('FAKE_PREMISE', 'right')}\\n")
    print(json.dumps({"type": "result", "total_cost_usd": float(os.environ.get("FAKE_COST", "0.25")),
                      "permission_denials": json.loads(os.environ.get("FAKE_DENIED", "[]"))}))
""")

GATE_OK = f'"{sys.executable}" -c "pass"'
GATE_FAIL = f'"{sys.executable}" -c "raise SystemExit(1)"'
TASK = """# {title}
Queue: {queue}
Priority: {priority}
Weight: light
Kind: {kind}
Lane: fake
Written: {written}

Do the thing.{extra}

## Gate
It works.

```
{gate}
```
"""
FAKE_ENV = ("FAKE_RESULT", "FAKE_HUMAN", "FAKE_SLEEP", "FAKE_COMMIT", "FAKE_STRAY", "FAKE_JUNK",
            "FAKE_JUNK_CHECK", "FAKE_PREMISE", "FAKE_DENIED", "FAKE_COST")


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        g("init", "-q", "-b", "main")
        g("config", "user.name", "t"); g("config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text(".review/*\n.venv\n")
        (self.repo / "orchestra").mkdir()
        shutil.copy(HERE / "review_prompt.md", self.repo / "orchestra" / "review_prompt.md")
        g("add", "."); g("commit", "-qm", "init")
        (self.repo / ".venv").mkdir()
        (self.repo / ".venv" / "keep.txt").write_text("real venv")
        (self.repo / ".review").mkdir()
        (self.repo / ".review" / "lanes.json").write_text(json.dumps({"fake": [sys.executable, "-c", FAKE]}))
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_BACKOFF_MIN"] = "0"
        os.environ["ORCHESTRA_TIMEOUT_S"] = json.dumps({"light": 3, "heavy": 6})
        for k in FAKE_ENV:
            os.environ.pop(k, None)
        import runner
        self.R = importlib.reload(runner)

    def tearDown(self):
        for wt in (self.R.WORKTREES.glob("*") if self.R.WORKTREES.exists() else []):
            self.R.unlink_link(wt / ".venv")
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, slug, title="t", queue="auto", priority="normal", kind="work", written=None,
            gate=GATE_OK, extra=""):
        p = self.R.QUEUE / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        written = written or dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
        p.write_text(TASK.format(title=title, queue=queue, priority=priority, kind=kind,
                                 written=written, gate=gate, extra=extra))
        return p

    def state(self):
        return json.loads(self.R.STATE_FILE.read_text())

    def why(self, slug):
        t = next(t for t in self.R.load_queue() if t.slug == slug)
        return self.R.why_not_runnable(t, self.R.load_state())

    # --- PASS is checked, not trusted

    def test_pass_with_green_runner_gate_merges_and_archives(self):
        self.add("a")
        os.environ["FAKE_COMMIT"] = "1"
        self.assertEqual(self.R.tick(), "PASS")
        day = next(self.R.ARCHIVE.iterdir())
        self.assertTrue((day / "a.md").exists() and (day / "a.report.md").exists())
        self.assertTrue((self.repo / "done.txt").exists(), "branch should be merged")
        self.assertIn("pass", self.state()["tasks"]["a"]["history"][-1]["runner_gate"])
        self.assertTrue((self.repo / ".venv" / "keep.txt").exists(), "removing the worktree must not touch the real venv")

    def test_pass_with_red_runner_gate_is_rejected(self):
        self.add("b", gate=GATE_FAIL)
        os.environ["FAKE_COMMIT"] = "1"
        self.assertEqual(self.R.tick(), "PASS-REJECTED")
        self.assertFalse((self.repo / "done.txt").exists())
        self.assertIn("runner's own gate check: fail", (self.R.PARKED / "b.why.txt").read_text())

    def test_pass_without_command_block_is_not_merged(self):
        p = self.add("c")
        p.write_text(p.read_text().split("```")[0] + "Looks right.\n")
        self.assertEqual(self.R.tick(), "PASS-REJECTED")
        self.assertIn("none", (self.R.PARKED / "c.why.txt").read_text())

    def test_unrelated_dirty_file_does_not_block_merge(self):
        (self.repo / "notes.txt").write_text("v1")
        subprocess.run(["git", "add", "notes.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "notes"], cwd=self.repo, check=True)
        (self.repo / "notes.txt").write_text("my own edit")  # Chuen's uncommitted, unrelated work
        self.add("u")
        os.environ["FAKE_COMMIT"] = "1"
        self.assertEqual(self.R.tick(), "PASS")
        self.assertTrue((self.repo / "done.txt").exists(), "merged despite the unrelated edit")
        self.assertEqual((self.repo / "notes.txt").read_text(), "my own edit")

    def test_dirty_file_the_branch_touches_blocks_merge(self):
        (self.repo / "done.txt").write_text("v1")
        subprocess.run(["git", "add", "done.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "done"], cwd=self.repo, check=True)
        (self.repo / "done.txt").write_text("my own edit")
        self.add("v")
        os.environ["FAKE_COMMIT"] = "1"
        self.assertEqual(self.R.tick(), "PASS")
        self.assertIn("done.txt", self.state()["tasks"]["v"]["merge"])
        self.assertEqual((self.repo / "done.txt").read_text(), "my own edit")

    def test_main_checkout_write_blocks_merge(self):
        self.add("w")
        os.environ["FAKE_STRAY"] = str(self.repo / "stray.txt")
        self.assertEqual(self.R.tick(), "PASS-REJECTED")
        self.assertIn("stray.txt", (self.R.PARKED / "w.why.txt").read_text())

    def test_same_content_does_not_run_twice(self):
        p = self.add("d")
        text = p.read_text()
        self.R.tick()
        (self.R.QUEUE / "d2.md").write_text(text)
        self.assertIn("already PASSed", self.why("d2"))

    # --- no blind retries

    def test_partial_and_fail_park_at_once(self):
        self.add("e")
        os.environ["FAKE_RESULT"] = "FAIL"
        self.assertEqual(self.R.tick(), "FAIL")
        self.assertIn("blind re-run", (self.R.PARKED / "e.why.txt").read_text())
        self.assertEqual(self.R.tick(), "idle")

    def test_human_line_parks(self):
        self.add("h")
        os.environ["FAKE_RESULT"] = "PARTIAL"
        os.environ["FAKE_HUMAN"] = "sign in to Outlook"
        self.R.tick()
        self.assertIn("waiting on Chuen", (self.R.PARKED / "h.why.txt").read_text())
        self.assertIn("sign in to Outlook", (self.R.REVIEW / "STATE_NOW.md").read_text())

    def test_timeout_retries_once_in_a_fresh_worktree_then_parks(self):
        self.add("t")
        os.environ["FAKE_SLEEP"] = "30"
        os.environ["FAKE_JUNK"] = "1"
        self.assertEqual(self.R.tick(), "TIMEOUT")
        flag = self.tmp / "reused.txt"
        os.environ["FAKE_JUNK_CHECK"] = str(flag)
        self.assertEqual(self.R.tick(), "TIMEOUT")
        self.assertFalse(flag.exists(), "attempt 2 must not start on attempt 1's leftovers")
        self.assertTrue((self.R.PARKED / "t.md").exists())
        h = self.state()["tasks"]["t"]["history"][-1]
        self.assertLess(h["seconds"], 15)
        self.assertIn("estimated", h["spent"])

    def test_no_report_is_infra(self):
        self.add("n")
        os.environ["FAKE_RESULT"] = "NONE"
        self.assertEqual(self.R.tick(), "NO_REPORT")
        self.assertFalse((self.R.PARKED / "n.md").exists(), "one retry first")

    def test_editing_a_parked_task_gives_it_a_fresh_start(self):
        p = self.add("f")
        os.environ["FAKE_RESULT"] = "FAIL"
        self.R.tick()
        parked = self.R.PARKED / "f.md"
        p.write_text(parked.read_text().replace("Do the thing.", "Do the thing, properly."))
        parked.unlink()
        self.assertIsNone(self.why("f"))

    # --- picking and limits

    def test_urgent_then_work_then_maintenance(self):
        self.add("1-maint", kind="maintenance", extra=" 1")
        self.add("2-work", extra=" 2")
        self.add("3-urgent", priority="urgent", extra=" 3")
        st = self.R.load_state()
        self.assertEqual(self.R.pick(self.R.load_queue(), st).slug, "3-urgent")
        (self.R.QUEUE / "3-urgent.md").unlink()
        self.assertEqual(self.R.pick(self.R.load_queue(), st).slug, "2-work")

    def test_maintenance_cap(self):
        self.add("m", kind="maintenance")
        st = self.R.load_state()
        u = self.R.today_usage(st)
        u.update(work_s=3600, maint_s=1800)
        self.assertIsNone(self.R.pick(self.R.load_queue(), st))
        u.update(work_s=36000, maint_s=1800)
        self.assertEqual(self.R.pick(self.R.load_queue(), st).slug, "m")

    def test_spend_is_recorded_and_capped(self):
        self.add("s1", extra=" 1")
        os.environ["FAKE_COST"] = "4.0"
        self.R.tick()
        self.assertAlmostEqual(self.R.today_usage(self.R.load_state())["cost_usd"], 4.0)
        self.add("s2", extra=" 2")
        os.environ["FAKE_COST"] = "1.5"
        self.R.tick()
        self.add("s3", extra=" 3")
        self.assertIn("spend cap", self.why("s3"))

    def test_timeout_comes_from_measured_runs(self):
        st = self.R.load_state()
        self.assertEqual(self.R.timeout_for("light", st), 3)
        st["durations"]["light"] = [200] * 20
        self.assertEqual(self.R.timeout_for("light", st), 600)

    def test_bad_header_stale_draft_placeholder(self):
        self.add("x1", priority="high", extra=" 1")
        self.add("x2", written=(dt.datetime.now() - dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M"), extra=" 2")
        self.add("x3", queue="draft", extra=" 3")
        p = self.add("x4", extra=" 4")
        p.write_text(p.read_text().replace("It works.", "<Exactly how>").replace("## Gate\n", "## Gate\n<x>\n"))
        self.assertIn("bad header", self.why("x1"))
        self.assertIn("stale", self.why("x2"))
        self.assertEqual(self.why("x3"), "Queue is not auto")
        self.assertIn("placeholder", self.why("x4"))
        self.assertEqual(self.R.tick(), "idle")

    def test_lock_is_exclusive(self):
        self.add("k")
        self.R.LOCK_FILE.write_text("{}")
        self.assertEqual(self.R.tick(), "busy")

    def test_premise_and_denials_reach_state_now(self):
        self.add("p")
        os.environ["FAKE_RESULT"] = "PARTIAL"
        os.environ["FAKE_PREMISE"] = "wrong: it was already fixed"
        os.environ["FAKE_DENIED"] = json.dumps([{"tool_name": "Bash", "tool_input": {"command": "npm test"}}])
        self.R.tick()
        h = self.state()["tasks"]["p"]["history"][-1]
        self.assertEqual(h["denied"], ["Bash(npm)"])
        sn = (self.R.REVIEW / "STATE_NOW.md").read_text()
        self.assertIn("refused Bash(npm)", sn)
        self.assertIn("premise was wrong", sn)

    def test_claude_lane_gets_the_guard_settings(self):
        wt = self.tmp / "wt"
        (wt / ".review").mkdir(parents=True)
        s = json.loads(self.R.hook_settings(wt).read_text())
        cmd = s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn("guard.py", cmd)


class GuardTest(unittest.TestCase):
    main = os.path.abspath("/x/GitHub/denisse")
    wt = os.path.abspath("/x/GitHub/denisse-wt/task-1")

    def d(self, tool, **inp):
        return guard.decide({"tool_name": tool, "tool_input": inp, "cwd": self.wt}, self.wt, self.main)

    def test_writes(self):
        self.assertIsNone(self.d("Write", file_path=os.path.join(self.wt, "a.py")))
        self.assertIsNone(self.d("Edit", file_path="sub/a.py"))
        why = self.d("Write", file_path=os.path.join(self.main, "a.py"))
        self.assertIn("instead", why)
        self.assertIsNotNone(self.d("Write", file_path=os.path.abspath("/elsewhere/x.txt")))

    def test_push_only_counts_as_a_command(self):
        self.assertIsNotNone(self.d("Bash", command="git status && git push"))
        self.assertIsNotNone(self.d("Bash", command="git -c user.name=x push origin"))
        self.assertIsNone(self.d("Bash", command="cat > r.md <<'EOF'\nNot pushed (the runner merges; git push is Chuen's)\ngit push stays with Chuen\nEOF\ngit status --short"))
        self.assertIsNotNone(self.d("Bash", command="cat > r.md <<'EOF'\nx\nEOF\ngit push"))
        self.assertIsNone(self.d("Bash", command="git commit -m 'never git push from a task'"))

    def test_bash(self):
        for cmd in ("git push origin master", "git -C ../denisse commit -m x", "git reset --hard HEAD~1",
                    "git clean -fdx", "gh pr create", "rm -rf build", f"cd {self.main} && ls"):
            self.assertIsNotNone(self.d("Bash", command=cmd), cmd)
        for cmd in ("git status", "git commit -m 'fix'", ".venv/Scripts/python.exe -m pytest",
                    f"ls {self.wt}", "python scripts/test_tasks.py"):
            self.assertIsNone(self.d("Bash", command=cmd), cmd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
