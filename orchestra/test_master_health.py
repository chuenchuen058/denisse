"""Tests for orchestra/master_health.py: master's own baseline check and dispatch that respects it.

    .venv\\Scripts\\python.exe orchestra\\test_master_health.py
Temp git repos (testing_git), a fake baseline command committed into each one (so a commit is
red or green by its own content), a fake spawn for the background start. No model calls.
"""
# checks: slow  (~40s: real git repos, detached worktrees, one real background child)
import datetime as dt
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ["ORCHESTRA_LIVE_WINDOW"] = "false"
os.environ["ORCHESTRA_TOASTS"] = "false"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent))  # testing_git; appended so it never shadows a local module
import testing_git  # noqa: E402

# The fake baseline: prints what run_checks.py prints for each module that did not pass, plus
# a warning that must never be read as a module, and exits 1 if anything failed.
FAKE_CHECKS = """import sys
from pathlib import Path
bad = [l.strip() for l in Path("failing.txt").read_text().splitlines() if l.strip()]
print("DeprecationWarning: fitz.something is deprecated")
print("ResourceWarning: unclosed file <_io.TextIOWrapper name='planner.py'>")
for m in bad:
    print(f"\\n--- {m} (FAIL, exit 1) ---")
    print("Traceback: AssertionError")
print("\\n3 modules: " + ("1 fail" if bad else "3 pass"))
sys.exit(1 if bad else 0)
"""
UNPARSED_CHECKS = 'import sys\nprint("something odd went wrong")\nsys.exit(2)\n'
TASK = """# {slug}
Queue: auto
Priority: normal
Weight: light
Kind: {kind}
Lane: fake
Written: {written}

Do {slug}.

## Gate
```
{gate}
```
"""


class MasterHealthTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)  # first: no inherited GIT_* reaches a real repo
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        testing_git.init_repo(self.repo, "-b", "main")
        (self.repo / ".gitignore").write_text(".review/*\n!.review/TEMPLATE.md\n.venv\n")
        (self.repo / "fake_checks.py").write_text(FAKE_CHECKS)
        (self.repo / "failing.txt").write_text("")
        (self.repo / ".review").mkdir()
        shutil.copy(HERE.parent / ".review" / "TEMPLATE.md", self.repo / ".review" / "TEMPLATE.md")
        self.git("add", ".")
        self.git("commit", "-qm", "init")
        (self.repo / ".venv").mkdir()
        (self.repo / ".venv" / "keep.txt").write_text("real venv")
        (self.repo / ".review" / "lanes.json").write_text(json.dumps({"fake": [sys.executable, "-c", "pass"]}))
        self.env = {k: os.environ.get(k) for k in ("ORCHESTRA_REPO", "ORCHESTRA_POWER",
                                                   "ORCHESTRA_MASTER_HEALTH_BASELINE",
                                                   "ORCHESTRA_MASTER_HEALTH_HEAD_CACHE_S",
                                                   "ORCHESTRA_BUGFIX_LANE")}
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_POWER"] = "ac"
        os.environ["ORCHESTRA_MASTER_HEALTH_BASELINE"] = json.dumps([f'"{sys.executable}" fake_checks.py'])
        os.environ["ORCHESTRA_MASTER_HEALTH_HEAD_CACHE_S"] = "0"
        os.environ["ORCHESTRA_BUGFIX_LANE"] = "fake"
        import runner
        self.R = importlib.reload(runner)
        import master_health
        self.M = importlib.reload(master_health)
        self.R.master_health = self.M
        self.M.forget_head()
        self.spawned = []
        self.R.aftercare_on = lambda: False  # never a vault note, push or autopilot from a test
        self.M.enabled = lambda R: bool(R.C["master_health"])  # the tests switch the real-repo-only gate on

    def tearDown(self):
        for wt in (self.R.WORKTREES.glob("*") if self.R.WORKTREES.exists() else []):
            self.R.unlink_link(wt / ".venv")
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- helpers

    def git(self, *a, cwd=None):
        return subprocess.run(["git", *a], cwd=cwd or self.repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    def head(self):
        return self.git("rev-parse", "HEAD")

    def commit_failing(self, *modules):
        (self.repo / "failing.txt").write_text("\n".join(modules) + "\n")
        self.git("add", "failing.txt")
        self.git("commit", "-qm", f"failing: {modules}")
        return self.head()

    def add(self, slug, kind="work"):
        p = self.R.QUEUE / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TASK.format(slug=slug, kind=kind, gate=f'"{sys.executable}" fake_checks.py',
                                 written=dt.datetime.now().strftime("%Y-%m-%dT%H:%M")))
        return next(t for t in self.R.load_queue() if t.slug == slug)

    def fake_spawn(self, argv, env):
        self.spawned.append(argv)
        return 4242

    def step(self, **kw):
        return self.M.step(self.R, self.R.load_state(), spawn_fn=self.fake_spawn, **kw)

    def red_master(self, *modules):
        commit = self.commit_failing(*modules)
        rec = self.M.run_check(self.R, commit)
        self.assertEqual(rec["status"], "red")
        return commit

    # --- records

    def test_green_record_from_a_baseline_run_and_its_worktree_is_gone(self):
        commit = self.head()
        rec = self.M.run_check(self.R, commit)
        self.assertEqual(rec["status"], "green")
        self.assertEqual(rec["how"], "baseline")
        self.assertEqual(self.M.record(self.R, commit)["status"], "green")
        self.assertFalse(any(self.R.WORKTREES.glob("master-health-*")), "the baseline worktree is removed")
        self.assertNotIn("master-health-", self.git("worktree", "list"))
        self.assertTrue((self.R.REPO / ".venv" / "keep.txt").exists(), "the real venv survives the unlink")

    def test_red_record_names_the_modules_from_run_checks_output_not_the_warnings(self):
        commit = self.red_master("scripts/test_planner_multistep.py", "orchestra/test_config.py")
        rec = self.M.record(self.R, commit)
        self.assertEqual(rec["modules"], ["scripts/test_planner_multistep.py", "orchestra/test_config.py"])
        self.assertIn("Traceback", rec["tail"])
        self.assertFalse(any("Warning" in m for m in rec["modules"]))
        self.assertIn("MASTER CHECK", self.R.LOG_FILE.read_text(encoding="utf-8"))

    def unparsed_commit(self):
        (self.repo / "fake_checks.py").write_text(UNPARSED_CHECKS)
        self.git("add", "fake_checks.py")
        self.git("commit", "-qm", "odd")
        return self.head()

    def test_a_failure_naming_no_module_is_broken_keeps_the_tail_and_holds_nothing(self):
        # The soak, 10 Nov 2026: cmd.exe could not find the venv python (exit 1, no module lines)
        # and master was held red on the environment. That is broken, not red.
        rec = self.M.run_check(self.R, self.unparsed_commit())
        self.assertEqual((rec["status"], rec["modules"]), ("broken", []))
        self.assertIn("something odd went wrong", rec["tail"])
        self.assertIsNone(self.R.why_not_runnable(self.add("a-work"), self.R.load_state()))
        self.assertEqual(self.step(), "broken", "not re-run at once")

    def test_with_unparsed_holds_it_is_red_and_says_unparsed(self):
        self.R.C["master_health_unparsed_holds"] = True
        rec = self.M.run_check(self.R, self.unparsed_commit())
        self.assertEqual(rec["status"], "red")
        self.assertEqual(self.M.describe_modules(self.R, rec), self.R.C["master_health_unparsed"])
        self.assertEqual(self.R.why_not_runnable(self.add("a-work"), self.R.load_state()),
                         "master is red: " + self.R.C["master_health_unparsed"])

    def test_a_broken_record_is_checked_again_after_the_retry_window(self):
        commit = self.head()
        old = self.R.now() - dt.timedelta(minutes=self.R.C["master_health_broken_retry_min"] + 1)
        self.M.save_record(self.R, commit, {"status": "broken", "modules": [], "tail": "timed out",
                                            "checked": old.isoformat(), "how": "baseline"})
        self.assertEqual(self.step(), "started 4242")

    def test_a_timed_out_or_broken_baseline_is_broken_not_red_and_holds_nothing(self):
        rec = self.M.judge(self.R, None, "slow...")
        self.assertEqual(rec["status"], "broken")
        self.M.save_record(self.R, self.head(), {**rec, "checked": self.R.now().isoformat(), "how": "baseline"})
        self.assertIsNone(self.R.why_not_runnable(self.add("a-work"), self.R.load_state()))

    # --- dispatch

    def test_red_master_holds_work_but_not_a_bug_fix_and_moving_on_releases_it(self):
        self.red_master("scripts/test_bad.py")
        state = self.R.load_state()
        work, fix = self.add("a-work"), self.add("b-fix", kind="bugfix")
        self.assertEqual(self.R.why_not_runnable(work, state), "master is red: scripts/test_bad.py")
        self.assertIsNone(self.R.why_not_runnable(fix, state))
        self.assertEqual(self.R.pick(self.R.load_queue(), state).slug, "b-fix")
        # The fix lands: master is at a new commit with no record yet -> counts as green.
        self.commit_failing()
        self.assertIsNone(self.R.why_not_runnable(work, state))

    def test_a_green_record_holds_nothing(self):
        self.M.run_check(self.R, self.head())
        self.assertIsNone(self.R.why_not_runnable(self.add("a-work"), self.R.load_state()))

    def test_switched_off_holds_nothing(self):
        self.red_master("scripts/test_bad.py")
        self.R.C["master_health"] = False
        self.assertIsNone(self.R.why_not_runnable(self.add("a-work"), self.R.load_state()))
        self.assertEqual(self.step(), "off")

    # --- when the check runs

    def test_an_unknown_commit_counts_as_green_and_starts_one_background_check(self):
        work = self.add("a-work")
        self.assertIsNone(self.R.why_not_runnable(work, self.R.load_state()))
        self.assertEqual(self.step(), "started 4242")
        self.assertEqual(len(self.spawned), 1)
        argv = self.spawned[0]
        self.assertIn(str(HERE / "master_health.py"), argv)
        self.assertEqual(argv[argv.index("--commit") + 1], self.head())
        self.assertTrue(self.M.lock_held(self.R))
        # The next tick, while that run is going: no second one, and still not held.
        self.assertIn("in progress", self.step())
        self.assertEqual(len(self.spawned), 1)
        self.assertIsNone(self.R.why_not_runnable(work, self.R.load_state()))
        self.assertIn("MASTER CHECK started pid=4242", self.R.LOG_FILE.read_text(encoding="utf-8"))

    def test_a_stale_lock_is_replaced(self):
        lock = self.R.REVIEW / self.R.C["master_health_lock"]
        old = self.R.now() - dt.timedelta(seconds=self.R.C["master_health_timeout_s"]
                                          + self.R.C["master_health_lock_grace_s"] + 60)
        lock.write_text(json.dumps({"since": old.isoformat(), "token": "dead"}))
        self.assertEqual(self.step(), "started 4242")

    def test_nothing_starts_on_battery(self):
        os.environ["ORCHESTRA_POWER"] = "battery:80"
        self.R.C["dispatch_on_battery"] = False
        self.assertIn("not now", self.step())
        self.assertEqual(self.spawned, [])
        self.assertFalse(self.M.lock_held(self.R))

    def test_nothing_starts_while_paused(self):
        self.R.campaign.pause(self.R.REVIEW)
        self.assertIn("not now", self.step())
        self.assertEqual(self.spawned, [])

    def test_a_failed_spawn_releases_the_lock(self):
        def boom(argv, env):
            raise OSError("no")
        self.assertIn("error", self.M.step(self.R, {}, spawn_fn=boom))
        self.assertFalse(self.M.lock_held(self.R))

    def test_a_tick_starts_the_check_and_does_not_wait_for_it(self):
        self.M.spawn = self.fake_spawn
        t0 = time.monotonic()
        self.assertEqual(self.R.tick(), "idle")
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(len(self.spawned), 1)

    def test_the_real_background_child_records_the_result_and_releases_the_lock(self):
        commit = self.commit_failing("scripts/test_bad.py")
        token = self.M.claim(self.R, commit)
        argv = self.M.child_argv(self.R, commit, token)
        r = subprocess.run(argv, cwd=HERE.parent, env={**os.environ}, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.M.record(self.R, commit)["modules"], ["scripts/test_bad.py"])
        self.assertFalse(self.M.lock_held(self.R))
        self.assertFalse((self.R.REVIEW / self.R.C["master_health_lock"]).exists())

    def test_after_a_fast_forward_the_combined_tree_result_is_recorded_without_a_run(self):
        commit = self.head()
        self.M.after_merge(self.R, commit, ["gate: pass: 1 command(s) exited 0"])
        self.assertIsNone(self.M.record(self.R, commit), "no baseline in the checks: not recorded")
        self.M.after_merge(self.R, commit, ["gate: pass: 1 command(s) exited 0",
                                            "run_checks --fast: pass: run_checks.py --fast exited 0"])
        rec = self.M.record(self.R, commit)
        self.assertEqual((rec["status"], rec["how"]), ("green", "combined"))
        self.assertEqual(self.step(), "green")
        self.assertEqual(self.spawned, [])

    # --- the bug-fix task

    def test_red_master_writes_one_urgent_bugfix_task_and_never_a_second(self):
        commit = self.red_master("scripts/test_bad.py")
        out = self.step()
        self.assertIn("wrote", out)
        fixes = [t for t in self.R.load_queue() if self.R.is_bugfix(t)]
        self.assertEqual(len(fixes), 1)
        fix = fixes[0]
        self.assertTrue(fix.auto)
        self.assertEqual(fix.priority, "urgent")
        self.assertEqual(fix.fields.get("from"), self.R.C["master_health_task_source"])
        text = fix.path.read_text(encoding="utf-8")
        self.assertIn(commit, text)
        self.assertIn("scripts/test_bad.py", text)
        self.assertEqual(fix.gate_commands, self.M.baseline(self.R))
        self.assertIsNone(self.R.why_not_runnable(fix, self.R.load_state()), "the fix itself may run")
        # Open: not a second one, however many ticks.
        self.assertIn("bug fix queued", self.step())
        self.assertIn("bug fix queued", self.step())
        # Parked (gone from the queue) on the same red commit: still not a second.
        self.R.park(fix, "PARTIAL: test", None)
        self.assertIn("already written", self.step())
        self.assertEqual(len([t for t in self.R.load_queue() if self.R.is_bugfix(t)]), 0)
        self.assertEqual(self.R.LOG_FILE.read_text(encoding="utf-8").count("MASTER RED"), 2,
                         "one red line (throttled) and one for the task written")

    def test_a_bugfix_task_that_cannot_be_written_is_tried_once_not_every_tick(self):
        (self.R.REVIEW / "TEMPLATE.md").unlink()
        self.red_master("scripts/test_bad.py")
        self.assertIn("could not write", self.step())
        self.assertIn("already written", self.step())
        self.assertEqual(list(self.R.QUEUE.glob("*.md")), [])

    def test_a_queued_bugfix_task_means_none_is_written(self):
        self.add("someone-elses-fix", kind="bugfix")
        self.red_master("scripts/test_bad.py")
        self.assertIn("bug fix queued: someone-elses-fix", self.step())
        self.assertEqual(len(list(self.R.QUEUE.glob("*.md"))), 1)

    def test_state_now_says_master_is_red(self):
        import state_now
        importlib.reload(state_now)
        state_now.R = self.R
        self.red_master("scripts/test_bad.py")
        self.add("a-work")
        text = state_now.build()
        self.assertIn("**Master:** RED", text)
        self.assertIn("master is red: scripts/test_bad.py", text)

    # --- park reasons

    def branch_from(self, slug, base):
        self.git("branch", f"review/{slug}", base)

    def test_park_reason_says_the_branch_is_not_at_fault_when_only_red_modules_failed(self):
        base = self.red_master("scripts/test_bad.py")
        self.commit_failing()  # master moved on (fixed) since the branch started
        task = self.add("a-work")
        self.branch_from("a-work", base)
        report = self.R.REPORTS / "a-work.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("RESULT: PASS\n")
        time.sleep(0.05)
        (self.R.REPORTS / "a-work.gate.log").write_text("--- scripts/test_bad.py (FAIL, exit 1) ---\n")
        self.R.park(task, "session said PASS, but runner's own gate check: fail", report)
        why = (self.R.PARKED / "a-work.why.txt").read_text(encoding="utf-8")
        self.assertIn(f"already red at this branch's base {base[:8]} on scripts/test_bad.py", why)
        self.assertIn("not at fault", why)

    def test_park_reason_does_not_excuse_a_new_failure(self):
        base = self.red_master("scripts/test_bad.py")
        task = self.add("a-work")
        self.branch_from("a-work", base)
        (self.R.REPORTS).mkdir(parents=True, exist_ok=True)
        (self.R.REPORTS / "a-work.gate.log").write_text(
            "--- scripts/test_bad.py (FAIL, exit 1) ---\n--- lib/test_new.py (FAIL, exit 1) ---\n")
        self.R.park(task, "gate failed", None)
        why = (self.R.PARKED / "a-work.why.txt").read_text(encoding="utf-8")
        self.assertIn("already red at this branch's base", why)
        self.assertNotIn("not at fault", why)

    def test_park_reason_unchanged_when_the_base_was_green(self):
        self.M.run_check(self.R, self.head())
        task = self.add("a-work")
        self.branch_from("a-work", self.head())
        self.R.park(task, "PARTIAL: a blind re-run won't change it (see report)", None)
        why = (self.R.PARKED / "a-work.why.txt").read_text(encoding="utf-8")
        self.assertNotIn("Master was already red", why)


if __name__ == "__main__":
    unittest.main()
