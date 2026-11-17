"""The runner dispatches and merges only while the main checkout is on the default branch
(orchestra/default_branch.py; audit 10 Nov 2026, O1). No model calls: throwaway git repos
and a fake lane, as in test_integration.py.

    .venv\\Scripts\\python.exe orchestra\\test_default_branch.py
"""
# checks: slow  (real git repos, fake lanes, combined-tree merges)
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent))
import test_integration as ti  # noqa: E402  the temp-repo fixture, fake lanes and helpers
import default_branch  # noqa: E402

ENV = ("ORCHESTRA_DEFAULT_BRANCH", "ORCHESTRA_DEFAULT_REMOTE")


class DefaultBranchTest(ti.IntegrationTest):
    """IntegrationTest's fixture (repo on 'main', no remote, fake lane) with config default_branch=main."""

    def setUp(self):
        os.environ["ORCHESTRA_DEFAULT_BRANCH"] = "main"
        super().setUp()

    def tearDown(self):
        super().tearDown()
        for k in ENV:
            os.environ.pop(k, None)

    def log_text(self):
        return self.R.LOG_FILE.read_text(encoding="utf-8") if self.R.LOG_FILE.exists() else ""

    def switch_off(self, detached=False):
        if detached:
            self.git("switch", "-q", "--detach", "HEAD")
        else:
            self.git("switch", "-q", "-c", "side")
            (self.repo / "side.txt").write_text("side")
            self.git("add", "side.txt")
            self.git("commit", "-qm", "side work")

    # --- on the default branch: everything as before

    def test_on_the_default_branch_a_tick_runs_and_merges(self):
        self.add("ok-task")
        self.assertEqual(self.run_tick({"done.txt": "x"}), "PASS")
        self.assertEqual(self.git("log", "-1", "--format=%s", "main"), "orchestra: merge review/ok-task")
        self.assertNotIn("OFF DEFAULT BRANCH", self.log_text())

    # --- off it: nothing dispatched, nothing fast-forwarded

    def _nothing_dispatched(self, detached):
        main_before = self.git("rev-parse", "main")
        self.switch_off(detached)
        here_before = self.head()
        self.add("waiting-task")
        self.assertEqual(self.run_tick({"done.txt": "x"}), "off default branch")
        self.assertEqual(self.run_tick({"done.txt": "x"}), "off default branch")
        self.assertFalse((self.R.WORKTREES / "waiting-task").exists(), "no worktree made")
        self.assertNotIn("START waiting-task", self.log_text())
        self.assertTrue((self.R.QUEUE / "waiting-task.md").exists(), "still queued")
        self.assertEqual(self.head(), here_before, "the checked-out tip is unchanged")
        self.assertEqual(self.git("rev-parse", "main"), main_before)
        self.assertEqual(self.log_text().count("OFF DEFAULT BRANCH"), 1, "logged once, not once per tick")
        what = "a detached HEAD" if detached else "'side'"
        self.assertIn(what, self.log_text())
        import state_now
        self.assertIn(f"**Default branch:** the main checkout is on {'' if detached else 'branch '}{what}",
                      state_now.build())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.R.tick(dry_run=True)
        self.assertIn("nothing would run or merge: the main checkout is on", out.getvalue())
        self.assertIn("would run: nothing", out.getvalue())
        # back on main: one line, then it dispatches again
        self.git("switch", "-q", "main")
        self.assertEqual(self.run_tick({"done.txt": "x"}), "PASS")
        self.assertIn("back on the default branch", self.log_text())
        self.assertNotIn("**Default branch:**", state_now.build())

    def test_on_another_branch_nothing_is_dispatched(self):
        self._nothing_dispatched(detached=False)

    def test_on_a_detached_head_nothing_is_dispatched(self):
        self._nothing_dispatched(detached=True)

    def _finished_lane_parks(self, detached):
        task, wt, branch = self.worked("done-lane", {"done.txt": "x"})
        branch_tip = self.git("rev-parse", branch)
        main_before = self.git("rev-parse", "main")
        self.switch_off(detached)
        here_before = self.head()
        result, ts = self.finish(task, wt, branch)
        self.assertEqual(result, "INTEGRATION-REJECTED")
        self.assertEqual(self.head(), here_before, "nothing fast-forwarded onto the checked-out tip")
        self.assertEqual(self.git("rev-parse", "main"), main_before, "main untouched")
        self.assertEqual(self.git("rev-parse", branch), branch_tip, "branch kept")
        why = (self.R.PARKED / "done-lane.why.txt").read_text(encoding="utf-8")
        self.assertIn("'main'", why)
        self.assertIn("a detached HEAD" if detached else "'side'", why)
        self.assertIn("requeue.py done-lane --merge-only", why)

    def test_a_finished_lane_on_another_branch_parks_with_its_branch_kept(self):
        self._finished_lane_parks(detached=False)

    def test_a_finished_lane_on_a_detached_head_parks_with_its_branch_kept(self):
        self._finished_lane_parks(detached=True)

    def test_a_switch_during_the_combined_tree_run_merges_nothing(self):
        task, wt, branch = self.worked("mid-switch", {"done.txt": "x"})
        main_before = self.git("rev-parse", "main")
        real = self.R.check_combined

        def switch_then_check(*a, **k):  # HEAD moved, so integrate() tests a second time
            if self.git("branch", "--show-current") == "main":
                self.switch_off(detached=False)
            return real(*a, **k)

        self.R.check_combined = switch_then_check
        try:
            result, _ = self.finish(task, wt, branch)
        finally:
            self.R.check_combined = real
        self.assertEqual(result, "INTEGRATION-REJECTED")
        self.assertEqual(self.git("rev-parse", "main"), main_before)
        self.assertEqual(self.git("log", "-1", "--format=%s", "side"), "side work", "side's tip unchanged")
        self.assertIn("'side'", (self.R.PARKED / "mid-switch.why.txt").read_text(encoding="utf-8"))

    # --- which branch is the default

    def test_no_remote_uses_the_config_key(self):
        self.assertEqual(default_branch.default_branch(self.repo, {"default_branch": "main"}), "main")
        self.assertEqual(default_branch.default_branch(self.repo, self.R.C), "main")
        self.assertIsNone(default_branch.default_branch(self.repo, {"default_branch": "nope"}),
                          "a branch that doesn't exist is unknown")
        self.assertIsNone(default_branch.off_default(self.repo, {"default_branch": "nope"}))
        self.git("switch", "-q", "-c", "side")
        self.assertIn("'side'", default_branch.off_default(self.repo, {"default_branch": "main"}))

    def test_the_remote_head_wins_over_the_config_key(self):
        self.git("branch", "trunk")
        self.git("update-ref", "refs/remotes/up/trunk", "trunk")
        self.git("symbolic-ref", "refs/remotes/up/HEAD", "refs/remotes/up/trunk")
        cfg = {"default_branch": "main", "default_remote": "up"}
        self.assertEqual(default_branch.default_branch(self.repo, cfg), "trunk")
        self.assertIn("'trunk'", default_branch.off_default(self.repo, cfg))
        self.assertEqual(default_branch.default_branch(self.repo, {"default_branch": "main"}), "main",
                         "another remote's HEAD is not used")

    def test_aftercare_push_uses_the_same_lookup(self):
        import aftercare
        self.git("switch", "-q", "-c", "side")
        self.assertEqual(aftercare.push(self.repo, {"default_branch": "main"}),
                         "not pushed: main checkout is on branch 'side', not 'main'")
        self.git("switch", "-q", "--detach", "HEAD")
        self.assertIn("a detached HEAD", aftercare.push(self.repo, {"default_branch": "main"}))

    def test_config_keys_are_mirrored_with_a_note(self):
        cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        defaults = json.loads((HERE / "config_defaults.json").read_text(encoding="utf-8"))
        for k in ("default_branch", "default_remote"):
            self.assertEqual(cfg[k], defaults[k], k)
        self.assertTrue(defaults.get("_default_branch") and cfg.get("_default_branch"))


def load_tests(loader, standard_tests, pattern):
    """Only this file's tests, not the inherited IntegrationTest ones."""
    names = [n for n in loader.getTestCaseNames(DefaultBranchTest) if n in DefaultBranchTest.__dict__]
    return unittest.TestSuite(DefaultBranchTest(n) for n in names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
