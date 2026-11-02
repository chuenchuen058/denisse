"""Tests for the runner's combined-tree integration and reviewer lane (no model calls).

    .venv\\Scripts\\python.exe -m unittest orchestra.test_integration -v
Each test builds a throwaway git repo with fake lanes, so the real queue is never touched.
"""
# checks: slow  (~18s: real git repos, fake lanes, combined-tree merges)
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

# Worker lane: writes FAKE_EDITS ({path: text}) into its worktree, commits, reports PASS.
FAKE_WORKER = textwrap.dedent("""
    import os, sys, subprocess, pathlib, json
    sys.stdin.read()
    for path, text in json.loads(os.environ.get("FAKE_EDITS", "{}")).items():
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        subprocess.run(["git", "add", path], check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fake work"], check=True)
    pathlib.Path(os.environ["ORCHESTRA_REPORT"]).write_text("RESULT: PASS\\nGATE: ok\\nHUMAN: none\\nPREMISE: right\\n")
    print(json.dumps({"type": "result", "total_cost_usd": 0.25}))
""")

# Reviewer lane: leaves a flag file (so a test can tell it ran), answers FAKE_VERDICT.
FAKE_REVIEWER = textwrap.dedent("""
    import os, sys, pathlib, json
    prompt = sys.stdin.read()
    pathlib.Path(os.environ["FAKE_REVIEW_FLAG"]).write_text(prompt)
    print(json.dumps({"type": "result", "result": os.environ.get("FAKE_VERDICT", "VERDICT: ok\\n- fine"),
                      "total_cost_usd": 0.10}))
""")

GATE = f'"{sys.executable}" -m unittest discover -s . -p "check_*.py"'
TASK = """# {slug}
Queue: auto
Priority: normal
Weight: light
Kind: work
Lane: fake
Written: {written}

Do {slug}.

## Gate
```
{gate}
```
"""
LIB = 'def greet():\n    return "hi"\n'
CHECK_LIB = 'import unittest, lib\n\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertEqual(lib.greet(), "hi")\n'
ENV = ("FAKE_EDITS", "FAKE_VERDICT", "FAKE_REVIEW_FLAG")


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text(".review/*\n.venv\n")
        (self.repo / "orchestra").mkdir()
        shutil.copy(HERE / "review_prompt.md", self.repo / "orchestra" / "review_prompt.md")
        (self.repo / "lib.py").write_text(LIB)
        (self.repo / "check_lib.py").write_text(CHECK_LIB)
        self.git("add", ".")
        self.git("commit", "-qm", "init")
        (self.repo / ".venv").mkdir()
        (self.repo / ".venv" / "keep.txt").write_text("real venv")
        (self.repo / ".review").mkdir()
        self.flag = self.tmp / "reviewed.txt"
        (self.repo / ".review" / "lanes.json").write_text(json.dumps({
            "fake": [sys.executable, "-c", FAKE_WORKER],
            "reviewer": [sys.executable, "-c", FAKE_REVIEWER]}))
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_DAILY_SPEND_CAP_USD"] = "50"
        os.environ["ORCHESTRA_TIMEOUT_S"] = json.dumps({"light": 30, "heavy": 60})
        os.environ["ORCHESTRA_POWER"] = "ac"  # never depend on whether the laptop is plugged in
        for k in ENV:
            os.environ.pop(k, None)
        os.environ["FAKE_REVIEW_FLAG"] = str(self.flag)
        import runner
        self.R = importlib.reload(runner)

    def tearDown(self):
        for wt in (self.R.WORKTREES.glob("*") if self.R.WORKTREES.exists() else []):
            self.R.unlink_link(wt / ".venv")
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ENV:
            os.environ.pop(k, None)

    # --- helpers

    def git(self, *a, cwd=None):
        return subprocess.run(["git", *a], cwd=cwd or self.repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    def head(self):
        return self.git("rev-parse", "HEAD")

    def add(self, slug, gate=GATE):
        p = self.R.QUEUE / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TASK.format(slug=slug, gate=gate, written=dt.datetime.now().strftime("%Y-%m-%dT%H:%M")))
        return p

    def worked(self, slug, files):
        """A task whose session ran from the current master and committed `files`, report PASS:
        what finish() sees. Lets two branches start from the same master."""
        self.add(slug)
        task = next(t for t in self.R.load_queue() if t.slug == slug)
        wt, branch = self.R.fresh_worktree(task)
        for path, text in files.items():
            (wt / path).parent.mkdir(parents=True, exist_ok=True)
            (wt / path).write_text(text)
            self.git("add", path, cwd=wt)
        self.git("commit", "-qm", f"{slug} work", cwd=wt)
        self.R.REPORTS.mkdir(parents=True, exist_ok=True)
        (self.R.REPORTS / f"{slug}.md").write_text("RESULT: PASS\nGATE: ok\nHUMAN: none\nPREMISE: right\n")
        return task, wt, branch

    def finish(self, task, wt, branch):
        st = self.R.load_state()
        before = self.R.main_checkout_status()
        result = self.R.finish(task, st, wt, branch, "ran", 1, before)
        self.R.save_state(st)
        return result, st["tasks"][task.slug]

    def master_green(self):
        r = subprocess.run(GATE, cwd=self.repo, shell=True, capture_output=True, text=True)
        return r.returncode == 0

    def run_tick(self, edits):
        os.environ["FAKE_EDITS"] = json.dumps(edits)
        return self.R.tick()

    def state(self):
        return json.loads(self.R.STATE_FILE.read_text())

    # --- combined tree

    def test_semantic_conflict_parks_the_second_branch_and_master_stays_green(self):
        # Both start from the same master and each passes its own gate.
        a = self.worked("a-rename", {"lib.py": 'def hello():\n    return "hi"\n',
                                     "check_lib.py": CHECK_LIB.replace("greet", "hello")})
        b = self.worked("b-caller", {"feature.py": "import lib\n\ndef shout():\n    return lib.greet().upper()\n",
                                     "check_feature.py": "import unittest, feature\n\nclass T(unittest.TestCase):\n"
                                                         "    def test(self):\n        self.assertEqual(feature.shout(), 'HI')\n"})
        self.assertEqual(self.finish(*a)[0], "PASS")
        after_a = self.head()
        self.assertTrue(self.master_green())

        result, ts = self.finish(*b)
        self.assertEqual(result, "INTEGRATION-REJECTED")
        self.assertIn("pass", ts["history"][-1]["runner_gate"], "b passed alone")
        self.assertEqual(self.head(), after_a, "master not moved by b")
        self.assertFalse((self.repo / "feature.py").exists())
        self.assertTrue(self.master_green(), "master stays green")
        why = (self.R.PARKED / "b-caller.why.txt").read_text()
        self.assertIn("combined tree", why)
        self.assertTrue((self.R.PARKED / "b-caller.md").exists())
        integ = ts["integration"]
        self.assertEqual(integ["base"], after_a)
        self.assertTrue(integ["commit"])
        self.assertTrue(integ["checks"][0].startswith("gate: fail"), integ["checks"])
        self.assertNotIn("merge", ts, "a parked task has no merge note for STATE NOW")
        self.assertTrue(self.git("rev-parse", "--verify", "review/b-caller"), "branch kept")
        self.assertFalse(any(p.name.endswith("--integration") for p in self.R.WORKTREES.iterdir()),
                         "integration worktree cleaned up")

    def test_master_is_fast_forwarded_to_the_tested_merge_commit(self):
        result, ts = self.finish(*self.worked("ff", {"done.txt": "x"}))
        self.assertEqual(result, "PASS")
        integ = ts["integration"]
        self.assertEqual(self.head(), integ["commit"], "master is exactly the commit that was tested")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "orchestra: merge review/ff")
        self.assertEqual(len(self.git("log", "-1", "--format=%P").split()), 2, "a --no-ff merge commit")
        self.assertEqual(integ["tries"], 1)
        self.assertTrue(any("run_checks --fast: skipped" in c for c in integ["checks"]))
        self.assertEqual(ts["merge"], "merged")
        self.assertEqual(ts["history"][-1]["integration"]["commit"], integ["commit"])
        self.assertTrue((self.repo / ".venv" / "keep.txt").exists(), "cleanup must not touch the real venv")

    def test_master_moved_while_testing_is_redone_once(self):
        task, wt, branch = self.worked("mv", {"done.txt": "x"})
        real, calls = self.R.check_combined, []

        def moving(t, w):
            calls.append(1)
            if len(calls) == 1:  # another lane merged while we tested
                (self.repo / "other.txt").write_text("o")
                self.git("add", "other.txt")
                self.git("commit", "-qm", "someone else")
            return real(t, w)
        self.R.check_combined = moving
        result, ts = self.finish(task, wt, branch)
        self.assertEqual(result, "PASS")
        self.assertEqual(ts["integration"]["tries"], 2)
        self.assertEqual(self.head(), ts["integration"]["commit"])
        self.assertTrue((self.repo / "other.txt").exists() and (self.repo / "done.txt").exists())
        self.assertIn("master moved while testing", self.R.LOG_FILE.read_text(encoding="utf-8"))

    def test_master_moved_twice_parks(self):
        task, wt, branch = self.worked("mv2", {"done.txt": "x"})
        real, n = self.R.check_combined, [0]

        def always_moving(t, w):
            n[0] += 1
            (self.repo / f"other{n[0]}.txt").write_text("o")
            self.git("add", f"other{n[0]}.txt")
            self.git("commit", "-qm", f"someone else {n[0]}")
            return real(t, w)
        self.R.check_combined = always_moving
        result, ts = self.finish(task, wt, branch)
        moved_to = self.head()
        self.assertEqual(result, "INTEGRATION-REJECTED")
        self.assertIn("master moved twice, retry", (self.R.PARKED / "mv2.why.txt").read_text())
        self.assertFalse((self.repo / "done.txt").exists())
        self.assertEqual(self.git("log", "-1", "--format=%s"), "someone else 2")
        self.assertEqual(moved_to, self.head())
        self.assertEqual(ts["integration"]["tries"], 2)

    def test_run_checks_fast_runs_in_the_combined_tree(self):
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "run_checks.py").write_text(
            "import sys, pathlib\nassert '--fast' in sys.argv\n"
            "sys.exit(1 if pathlib.Path('bad.txt').exists() else 0)\n")
        self.git("add", "scripts")
        self.git("commit", "-qm", "checks")
        result, ts = self.finish(*self.worked("ok-fast", {"done.txt": "x"}))
        self.assertEqual(result, "PASS")
        self.assertIn("run_checks --fast: pass: run_checks.py --fast exited 0", ts["integration"]["checks"])
        before = self.head()
        result, ts = self.finish(*self.worked("bad-fast", {"bad.txt": "x"}))
        self.assertEqual(result, "INTEGRATION-REJECTED")
        self.assertIn("run_checks.py --fast exited 1", (self.R.PARKED / "bad-fast.why.txt").read_text())
        self.assertEqual(self.head(), before)

    def test_run_checks_without_fast_option_is_skipped(self):
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "run_checks.py").write_text("raise SystemExit(1)\n")
        self.git("add", "scripts")
        self.git("commit", "-qm", "checks")
        result, ts = self.finish(*self.worked("nofast", {"done.txt": "x"}))
        self.assertEqual(result, "PASS")
        self.assertIn("run_checks --fast: skipped: scripts/run_checks.py has no --fast option",
                      ts["integration"]["checks"])

    def test_dirty_file_the_branch_touches_still_blocks(self):
        (self.repo / "done.txt").write_text("v1")
        self.git("add", "done.txt")
        self.git("commit", "-qm", "done")
        (self.repo / "done.txt").write_text("my own edit")
        result, ts = self.finish(*self.worked("dirty", {"done.txt": "x"}))
        self.assertEqual(result, "PASS")
        self.assertIn("uncommitted changes to done.txt", ts["merge"])
        self.assertEqual((self.repo / "done.txt").read_text(), "my own edit")

    # --- reviewer lane

    def test_non_risky_diff_skips_review(self):
        self.add("plain")
        self.assertEqual(self.run_tick({"done.txt": "x"}), "PASS")
        self.assertFalse(self.flag.exists(), "the reviewer must not run")
        ts = self.state()["tasks"]["plain"]
        self.assertEqual(ts["review"]["verdict"], "skipped")
        self.assertTrue((self.repo / "done.txt").exists())

    def test_reviewer_block_parks_with_the_review_attached(self):
        self.add("risky")
        os.environ["FAKE_VERDICT"] = "VERDICT: block\n- orchestra/thing.py:1 turns the guard off"
        self.assertEqual(self.run_tick({"orchestra/thing.py": "GUARD = False\n"}), "REVIEW-BLOCKED")
        prompt = self.flag.read_text()
        self.assertIn("orchestra/thing.py", prompt, "the reviewer is given the diff")
        self.assertIn("Do risky.", prompt, "and the task file")
        self.assertIn("RESULT: PASS", prompt, "and the report")
        self.assertNotIn("{{", prompt.split("## Diff")[0])
        self.assertFalse((self.repo / "orchestra" / "thing.py").exists(), "not merged")
        review = (self.R.PARKED / "risky.review.md").read_text()
        self.assertIn("turns the guard off", review)
        self.assertIn("reviewer blocked it", (self.R.PARKED / "risky.why.txt").read_text())
        ts = self.state()["tasks"]["risky"]
        self.assertEqual(ts["review"]["verdict"], "block")
        self.assertIn("orchestra/thing.py", ts["review"]["why"])
        self.assertEqual(ts["history"][-1]["review"]["verdict"], "block")
        cost = self.R.today_usage(self.R.load_state())["cost_usd"]
        self.assertAlmostEqual(cost, 0.35, msg="reviewer cost counts like any lane")

    def test_reviewer_ok_merges(self):
        self.add("risky-ok")
        self.assertEqual(self.run_tick({"orchestra/thing.py": "X = 1\n"}), "PASS")
        self.assertTrue(self.flag.exists())
        self.assertTrue((self.repo / "orchestra" / "thing.py").exists())
        ts = self.state()["tasks"]["risky-ok"]
        self.assertEqual(ts["review"]["verdict"], "ok")
        self.assertEqual(ts["merge"], "merged")

    def test_reviewer_without_verdict_blocks(self):
        self.add("mute")
        os.environ["FAKE_VERDICT"] = "Looks good to me!"
        self.assertEqual(self.run_tick({"atrium/hook.py": "x = 1\n"}), "REVIEW-BLOCKED")
        self.assertIn("no VERDICT line", self.state()["tasks"]["mute"]["review"]["reasons"])

    def test_review_switch_off_skips_review(self):
        self.add("off")
        self.R.C["review_risky"] = False
        self.assertEqual(self.run_tick({"orchestra/thing.py": "X = 1\n"}), "PASS")
        self.assertFalse(self.flag.exists())
        self.assertEqual(self.state()["tasks"]["off"]["review"]["why"], "review_risky is off")

    def test_risk_rules(self):
        n = [0]

        def risk_of(files):
            n[0] += 1
            branch = f"risk{n[0]}"
            self.git("checkout", "-qb", branch)
            for path, text in files.items():
                (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
                (self.repo / path).write_text(text)
                self.git("add", path)
            self.git("commit", "-qm", branch)
            self.git("checkout", "-q", "main")
            out = self.R.risk(branch)
            self.git("branch", "-D", branch)
            return out
        self.assertIsNone(risk_of({"done.txt": "x"}))
        self.assertIn("orchestra/a.py", risk_of({"orchestra/a.py": "x"}))
        self.assertIn("atrium/hook.py", risk_of({"atrium/hook.py": "x"}))
        self.assertIn("install_task.ps1", risk_of({"scripts/install_task.ps1": "x"}))
        self.assertIsNone(risk_of({"atrium/hook_notes.md": "x"}))
        self.assertIn("changed lines", risk_of({"big.txt": "x\n" * 401}))
        self.assertIsNone(risk_of({"big.txt": "x\n" * 400}))

    def test_parse_verdict(self):
        P = self.R.parse_verdict
        self.assertEqual(P(json.dumps({"type": "result", "result": "VERDICT: ok\n- fine"}))[0], "ok")
        self.assertEqual(P("**VERDICT: block**\n- bad")[0], "block")
        self.assertIsNone(P("I think it is fine")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
