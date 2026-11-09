"""Tests for orchestra/conflicts.py: predicted files, the dispatch hold, the merge follow-up.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_conflicts -v
Each test builds a throwaway git repo with fake lanes (no model calls); the real queue is
never touched.
"""
# checks: slow  (~20s: real git repos, worktrees, fake lanes)
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
# Never a real window or toast from a test (orchestra/visible.py: the live window and toasts).
os.environ["ORCHESTRA_LIVE_WINDOW"] = "false"
os.environ["ORCHESTRA_TOASTS"] = "false"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent))  # testing_git; appended so it never shadows a local module
import testing_git  # noqa: E402

# Worker lane: writes FAKE_EDITS ({path: text}) into its worktree, commits, reports PASS.
FAKE_WORKER = textwrap.dedent("""
    import os, sys, subprocess, pathlib, json
    sys.stdin.read()
    pathlib.Path(".review", "hook.log").write_text("fake hook")
    for path, text in json.loads(os.environ.get("FAKE_EDITS", "{}")).items():
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        subprocess.run(["git", "add", path], check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fake work"], check=True)
    pathlib.Path(os.environ["ORCHESTRA_REPORT"]).write_text("RESULT: PASS\\nGATE: ok\\nHUMAN: none\\nPREMISE: right\\n")
    print(json.dumps({"type": "result", "total_cost_usd": 0.25}))
""")

# Merger lane: what a <slug>-merge follow-up session does. Reads the branch to merge from its
# task file, merges it, resolves conflicts with FAKE_RESOLVE ({path: text}), reports PASS.
FAKE_MERGER = textwrap.dedent("""
    import os, sys, subprocess, pathlib, json, re
    sys.stdin.read()
    pathlib.Path(".review", "hook.log").write_text("fake hook")
    task = pathlib.Path(os.environ["ORCHESTRA_TASK"]).read_text(encoding="utf-8")
    branch = re.search(r"git merge --no-ff (review/\\S+)`", task).group(1)
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    r = subprocess.run(g + ["merge", "--no-ff", "-m", "merge " + branch, branch])
    if r.returncode != 0:
        for path, text in json.loads(os.environ.get("FAKE_RESOLVE", "{}")).items():
            pathlib.Path(path).write_text(text)
            subprocess.run(["git", "add", path], check=True)
        subprocess.run(g + ["commit", "-qm", "resolved"], check=True)
    pathlib.Path(os.environ["ORCHESTRA_REPORT"]).write_text("RESULT: PASS\\nGATE: ok\\nHUMAN: none\\nPREMISE: right\\n")
    print(json.dumps({"type": "result", "total_cost_usd": 0.20}))
""")

FAKE_REVIEWER = textwrap.dedent("""
    import sys, json
    sys.stdin.read()
    print(json.dumps({"type": "result", "result": "VERDICT: ok\\n- fine", "total_cost_usd": 0.10}))
""")

GATE_OK = f'"{sys.executable}" -c "pass"'
GATE_CHECKS = f'"{sys.executable}" -m unittest discover -s . -p "check_*.py"'
TASK = """# {title}
Queue: auto
Priority: {priority}
Weight: light
Kind: work
Lane: {lane}
Written: {written}

{body}

## Gate
```
{gate}
```
"""
LIB = 'def greet():\n    return "hi"\n' + "\n" * 30 + "def other():\n    return 1\n"
CHECK_LIB = 'import unittest, lib\n\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertEqual(lib.greet(), "hi")\n'
ENV = ("FAKE_EDITS", "FAKE_RESOLVE")


class ConflictsTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)  # first: no inherited GIT_* reaches a real repo
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text(".review/*\n.venv\n")
        for rel, text in {"orchestra/review_prompt.md": (HERE / "review_prompt.md").read_text(encoding="utf-8"),
                          "orchestra/runner.py": "# runner\n", "orchestra/README.md": "# orchestra\n",
                          "eval/cases.py": "CASES = []\n", "scripts/x.py": "x = 1\n", "README.md": "# repo\n",
                          "lib.py": LIB, "check_lib.py": CHECK_LIB, "done.txt": "base\n"}.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "init")
        (self.repo / ".venv").mkdir()
        (self.repo / ".review").mkdir()
        (self.repo / ".review" / "lanes.json").write_text(json.dumps({
            "fake": [sys.executable, "-c", FAKE_WORKER],
            "claude": [sys.executable, "-c", FAKE_MERGER],
            "reviewer": [sys.executable, "-c", FAKE_REVIEWER]}))
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_DAILY_SPEND_CAP_USD"] = "50"
        os.environ["ORCHESTRA_TIMEOUT_S"] = json.dumps({"light": 30, "heavy": 60})
        os.environ["ORCHESTRA_POWER"] = "ac"
        for k in ENV:
            os.environ.pop(k, None)
        import runner
        self.R = importlib.reload(runner)
        import conflicts
        self.X = importlib.reload(conflicts)

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

    def commit_master(self, files, msg="master moves"):
        for path, text in files.items():
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(text)
            self.git("add", path)
        self.git("commit", "-qm", msg)

    def add(self, slug, body="Do it.", priority="normal", gate=GATE_OK, lane="fake", title=None):
        p = self.R.QUEUE / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TASK.format(title=title or slug, priority=priority, lane=lane, body=body, gate=gate,
                                 written=dt.datetime.now().strftime("%Y-%m-%dT%H:%M")), encoding="utf-8")
        return p

    def task(self, slug):
        return next(t for t in self.R.load_queue() if t.slug == slug)

    def running(self, slug, edits=None, lock="runner.fake.lock"):
        """A task another slot is running: its lock names it, its worktree has `edits` uncommitted."""
        wt, _ = self.R.fresh_worktree(self.task(slug))
        for path, text in (edits or {}).items():
            (wt / path).write_text(text)
        (self.R.REVIEW / lock).write_text(json.dumps({"pid": 999999, "task": slug, "lane": "fake"}))
        return wt

    def worked(self, slug, files, gate=GATE_OK, body="Do it."):
        """A task whose session ran from the current master and committed `files`, report PASS."""
        self.add(slug, body=body, gate=gate)
        task = self.task(slug)
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
        result = self.R.finish(task, st, wt, branch, "ran", 1, self.R.main_checkout_status())
        self.R.save_state(st)
        return result, st["tasks"][task.slug]

    def pick(self, *slugs, dry_run=False):
        st = self.R.load_state()
        t = self.R.pick([self.task(s) for s in slugs], st, dry_run=dry_run)
        self.R.save_state(st)
        return t.slug if t else None

    def holds(self, slug):
        return self.R.load_state()["tasks"].get(slug, {}).get("conflict_holds", 0)

    def log(self):
        return self.R.LOG_FILE.read_text(encoding="utf-8") if self.R.LOG_FILE.exists() else ""

    # --- predict: paths named in the body

    def test_paths_in_text(self):
        known = {"orchestra/runner.py", "orchestra/status.py", "eval/cases.py", "README.md",
                 "orchestra/README.md", "orchestra/hooks/pre-push"}
        body = textwrap.dedent("""
            Edit `orchestra/runner.py` and orchestra\\status.py, then eval/cases.py. Update `README.md`.
            The hook is `orchestra/hooks/pre-push`. Branch `review/2026-11-02T2000-campaign-controls` is kept.
            Don't touch `.review/queue/x.md`, `.venv\\Scripts\\python.exe`, `orchestra/local_team/` or
            `orchestra/test_*.py`. See https://github.com/x/y/blob/main/eval/run_eval.py for context.
            Gate: .venv\\Scripts\\python.exe -m unittest orchestra.test_conflicts
        """)
        got = self.X.paths_in_text(body, known)
        self.assertEqual(got, {"orchestra/runner.py", "orchestra/status.py", "eval/cases.py",
                               "README.md", "orchestra/README.md", "orchestra/hooks/pre-push"})
        # without a file list, bare names stay bare and extension-less slash paths are kept
        self.assertEqual(self.X.paths_in_text("Fix `runner.py` and `orchestra/hooks/pre-push`."),
                         {"runner.py", "orchestra/hooks/pre-push"})
        self.assertEqual(self.X.paths_in_text("Nothing here (see docs)."), set())

    # --- predict: the learned map

    def test_learned_map_predicts_from_title_words(self):
        m = {"words": {}}
        self.X.learn(m, self.X.words_of("Fix the dispatch log line"), {"orchestra/runner.py", "orchestra/README.md"})
        self.X.learn(m, self.X.words_of("Battery dispatch pause"), {"orchestra/power.py", "orchestra/runner.py"})
        self.assertEqual(self.X.words_of("Fix the dispatch log line"), {"dispatch", "log", "line"})
        got = self.X.predicted_from_map(m, self.X.words_of("Dispatch on battery, quieter"))
        self.assertIn("orchestra/runner.py", got, "'dispatch' pointed at runner.py both times")
        self.assertIn("orchestra/power.py", got, "'battery' was seen once, with power.py")
        self.assertNotIn("orchestra/README.md", got, "'dispatch' -> README only half the time, no other vote")
        self.assertEqual(self.X.predicted_from_map(m, {"unknown"}), set())

    def test_map_is_bootstrapped_from_past_merges(self):
        self.git("checkout", "-qb", "review/2026-11-02T2000-campaign-controls")
        self.commit_master({"eval/cases.py": "CASES = [1]\n", "orchestra/config.json": "{}\n"}, "work")
        self.git("checkout", "-q", "main")
        self.git("merge", "--no-ff", "-m", "orchestra: merge review/2026-11-02T2000-campaign-controls",
                 "review/2026-11-02T2000-campaign-controls")
        m = self.X.load_map()
        self.assertTrue(self.X.map_file().exists())
        self.assertEqual(set(m["words"]), {"campaign", "controls"}, "the timestamp is not a word")
        self.assertEqual(set(m["words"]["campaign"]["files"]), {"eval/cases.py", "orchestra/config.json"})
        self.assertIn("eval/cases.py", self.X.predicted_from_map(m, self.X.words_of("Campaign controls v2")))

    # --- dispatch: hold on hot overlap, never starve

    def test_overlap_hold_then_runs_anyway(self):
        self.add("r", body="Rework `orchestra/runner.py`.")
        self.running("r", {"orchestra/runner.py": "# edited by r\n"})
        self.add("a", body="Add a flag to `orchestra/runner.py`.")
        self.add("b", body="Tidy `scripts/x.py`.")
        self.add("c", body="Tidy eval/run_eval.py.")
        self.add("d", body="Docs only, no paths.")
        self.assertEqual(self.pick("a", "b", dry_run=True), "b")
        self.assertEqual(self.holds("a"), 0, "a dry run counts nothing")
        self.assertEqual(self.pick("a", "b"), "b")
        self.assertEqual(self.holds("a"), 1)
        self.assertIn("held a: overlaps r on orchestra/runner.py (hold 1/2)", self.log())
        self.assertEqual(self.pick("a", "c"), "c")
        self.assertEqual(self.holds("a"), 2)
        self.assertEqual(self.pick("a", "d"), "a", "after 2 holds it runs even though d is free")
        self.assertIn("a runs anyway after 2 holds", self.log())
        self.assertEqual(self.holds("a"), 0, "reset once it runs")

    def test_predicted_overlap_holds_before_the_running_task_has_edited_anything(self):
        self.add("r", body="Add cases to `eval/cases.py`.")
        self.running("r")  # no edits yet: only its prediction counts
        self.add("a", body="More eval cases in eval/cases.py.")
        self.add("b", body="Tidy `scripts/x.py`.")
        self.assertEqual(self.pick("a", "b"), "b")
        self.assertIn("held a: overlaps r on eval/cases.py", self.log())

    def test_learned_prediction_holds_too(self):
        m = self.X.load_map()
        self.X.learn(m, self.X.words_of("Dispatch holds"), {"orchestra/runner.py"})
        self.X.save_map(m)
        self.add("r", body="Rework the lock.")
        self.running("r", {"orchestra/runner.py": "# edited by r\n"})
        self.add("a", title="Dispatch logging", body="Say more in the log.")  # names no path
        self.add("b", body="Tidy `scripts/x.py`.")
        self.assertEqual(self.pick("a", "b"), "b")
        self.assertIn("held a: overlaps r on orchestra/runner.py", self.log())

    def test_non_hot_overlap_and_no_alternative_do_not_hold(self):
        self.add("r", body="Rework `scripts/x.py` and `orchestra/runner.py`.")
        self.running("r", {"scripts/x.py": "x = 2\n", "orchestra/runner.py": "# r\n"})
        self.add("a", body="Also touch `scripts/x.py`.")
        self.add("b", body="Tidy `lib.py`.")
        self.assertEqual(self.pick("a", "b"), "a", "scripts/x.py is not a hot file")
        self.assertEqual(self.holds("a"), 0)
        self.add("e", body="Edit `orchestra/runner.py`.")
        self.add("f", body="Edit orchestra/config.json and `orchestra/runner.py`.")
        self.assertEqual(self.pick("e", "f"), "e", "everything overlaps: the first runs, nobody waits")
        self.assertEqual((self.holds("e"), self.holds("f")), (0, 0))
        self.assertIn("e runs although it overlaps r on orchestra/runner.py: no alternative", self.log())
        self.assertNotIn("held", self.log())

    def test_hot_patterns_and_config(self):
        hot = ["orchestra/runner.py", "README.md", "eval/*.py"]
        self.assertTrue(self.X.is_hot("orchestra/runner.py", hot))
        self.assertTrue(self.X.is_hot("orchestra/README.md", hot), "a name without a slash matches any README")
        self.assertTrue(self.X.is_hot("eval/cases.py", hot))
        self.assertFalse(self.X.is_hot("orchestra/status.py", hot))
        defaults = json.loads((HERE / "config_defaults.json").read_text(encoding="utf-8"))
        self.assertEqual(self.X.hot_patterns(), defaults["conflict_hot_files"])
        self.R.C["conflict_hot_files"] = ["scripts/x.py"]
        self.add("r", body="Rework `scripts/x.py`.")
        self.running("r", {"scripts/x.py": "x = 2\n"})
        self.add("a", body="Also `scripts/x.py`.")
        self.add("b", body="Tidy `lib.py`.")
        self.assertEqual(self.pick("a", "b"), "b", "the configured list is what counts")

    def test_nothing_running_changes_nothing(self):
        self.add("a", body="Edit `orchestra/runner.py`.")
        self.add("b", body="Edit `orchestra/runner.py` too.", priority="urgent")
        self.assertEqual(self.pick("a", "b"), "b", "plain priority order")
        self.assertEqual(self.pick("a"), "a")
        self.assertEqual((self.holds("a"), self.holds("b")), (0, 0))
        self.assertNotIn("held", self.log())
        self.assertFalse(self.X.map_file().exists(), "no git work at all when no other slot is busy")

    def test_a_broken_predictor_falls_back_to_plain_order(self):
        self.add("r", body="x")
        self.running("r")
        self.add("a", body="Edit `orchestra/runner.py`.")
        self.X.known_files = lambda: 1 / 0
        self.assertEqual(self.pick("a"), "a")
        self.assertIn("conflicts: ZeroDivisionError", self.log())

    # --- recover: one merge follow-up per original

    def test_merge_conflict_queues_one_followup(self):
        a = self.worked("a-task", {"done.txt": "branch\n"}, body="Rewrite `done.txt`.")
        self.commit_master({"done.txt": "master\n"})
        result, ts = self.finish(*a)
        self.assertEqual(result, "PASS")
        self.assertEqual(ts["merge"], "merge conflict; merge by hand; follow-up a-task-merge queued")
        self.assertEqual(ts["merge_followup"], "a-task-merge")
        self.assertEqual((self.repo / "done.txt").read_text(), "master\n", "master untouched")
        self.assertIn("FOLLOW-UP a-task-merge queued: merge conflict with current master", self.log())
        fu = self.task("a-task-merge")
        self.assertEqual((fu.priority, fu.lane, fu.fields["merge-of"], fu.weight), ("urgent", "claude", "a-task", "light"))
        self.assertEqual(fu.gate_commands, a[0].gate_commands, "the original gate, verbatim")
        text = fu.path.read_text(encoding="utf-8")
        self.assertIn("git merge --no-ff review/a-task`", text)
        self.assertIn("Files changed on both sides: `done.txt`", text)
        self.assertIn("Files the branch touches: `done.txt`", text)
        self.assertIsNone(self.R.why_not_runnable(fu, self.R.load_state()), "runnable as written")
        self.assertIsNone(self.R.header_problem(fu))
        # the same original again: no second follow-up
        st = self.R.load_state()
        self.assertIn("already exists", self.X.after_integrate(a[0], a[2], "merge conflict; merge by hand", st, {"done.txt"}))
        self.assertEqual(len(list(self.R.QUEUE.glob("a-task-merge*.md"))), 1)
        # a follow-up that itself conflicts does not chain into -merge-merge
        self.assertIn("a person must look", self.X.after_integrate(fu, "review/a-task-merge", "merge conflict; merge by hand", st, set()))
        self.assertFalse((self.R.QUEUE / "a-task-merge-merge.md").exists())

    def test_followup_merges_and_marks_the_original(self):
        a = self.worked("a-task", {"done.txt": "branch\n"})
        self.commit_master({"done.txt": "master\n"})
        self.assertEqual(self.finish(*a)[0], "PASS")
        self.assertTrue((self.R.QUEUE / "a-task-merge.md").exists())
        os.environ["FAKE_RESOLVE"] = json.dumps({"done.txt": "master\nbranch\n"})
        self.assertEqual(self.R.tick(), "PASS", "the follow-up ran on the claude lane and merged")
        self.assertEqual((self.repo / "done.txt").read_text(), "master\nbranch\n", "both sides kept")
        st = self.R.load_state()
        self.assertEqual(st["tasks"]["a-task-merge"]["merge"], "merged")
        self.assertEqual(st["tasks"]["a-task"]["merge"], "merged by a-task-merge")
        self.assertFalse((self.R.QUEUE / "a-task-merge.md").exists())
        self.assertNotIn("merge", self.X.load_map()["words"], "follow-ups teach the map nothing")

    def test_combined_tree_conflict_queues_followup_and_parks(self):
        # master renames greet(); the branch still calls greet() and edits the far end of lib.py:
        # the merge is clean, the combined gate fails, and both sides changed lib.py.
        b = self.worked("b-task", {"lib.py": LIB.replace("return 1", "return 2"),
                                   "check_other.py": "import unittest, lib\n\nclass T(unittest.TestCase):\n"
                                                     "    def test(self):\n        self.assertEqual(lib.greet(), 'hi')\n"
                                                     "        self.assertEqual(lib.other(), 2)\n"},
                        gate=GATE_CHECKS)
        self.commit_master({"lib.py": LIB.replace("greet", "hello"), "check_lib.py": CHECK_LIB.replace("greet", "hello")})
        result, ts = self.finish(*b)
        self.assertEqual(result, "INTEGRATION-REJECTED")
        why = (self.R.PARKED / "b-task.why.txt").read_text()
        self.assertIn("combined tree", why)
        self.assertIn("follow-up b-task-merge queued", why)
        self.assertTrue((self.R.PARKED / "b-task.md").exists(), "the original still parks")
        fu = self.task("b-task-merge")
        self.assertIn("both sides changed lib.py", fu.path.read_text(encoding="utf-8"))
        self.assertEqual(fu.gate_commands, [GATE_CHECKS])
        self.assertEqual(ts["merge_followup"], "b-task-merge")

    def test_red_tree_without_shared_files_is_not_a_conflict(self):
        c = self.worked("c-task", {"feature.py": "import lib\n\ndef shout():\n    return lib.greet().upper()\n",
                                   "check_feature.py": "import unittest, feature\n\nclass T(unittest.TestCase):\n"
                                                       "    def test(self):\n        self.assertEqual(feature.shout(), 'HI')\n"},
                        gate=GATE_CHECKS)
        self.commit_master({"lib.py": LIB.replace("greet", "hello"), "check_lib.py": CHECK_LIB.replace("greet", "hello")})
        result, ts = self.finish(*c)
        self.assertEqual(result, "INTEGRATION-REJECTED")
        self.assertFalse((self.R.QUEUE / "c-task-merge.md").exists(), "no shared file: a person decides")
        self.assertNotIn("merge_followup", ts)
        self.assertIsNone(self.X.conflict_reason("parked: master moved twice, retry", c[2]))
        self.assertIsNone(self.X.conflict_reason("main checkout has uncommitted changes to x; merge by hand", c[2]))

    def test_followups_can_be_switched_off(self):
        self.R.C["conflict_followup"] = False
        a = self.worked("a-task", {"done.txt": "branch\n"})
        self.commit_master({"done.txt": "master\n"})
        result, ts = self.finish(*a)
        self.assertEqual(result, "PASS")
        self.assertIn("merge follow-ups are off", ts["merge"])
        self.assertFalse((self.R.QUEUE / "a-task-merge.md").exists())

    def test_followup_values_come_from_config(self):
        self.R.C.update(conflict_followup_suffix="-join", conflict_followup_lane="fake",
                        conflict_followup_priority="normal")
        a = self.worked("a-task", {"done.txt": "branch\n"})
        self.commit_master({"done.txt": "master\n"})
        result, ts = self.finish(*a)
        self.assertEqual(ts["merge_followup"], "a-task-join")
        fu = self.task("a-task-join")
        self.assertEqual((fu.lane, fu.priority, fu.fields["merge-of"]), ("fake", "normal", "a-task"))

    def test_merged_followup_archives_a_parked_original(self):
        st = self.R.load_state()
        self.R.PARKED.mkdir(parents=True, exist_ok=True)
        for name in ("o.md", "o.why.txt", "o.report.md"):
            (self.R.PARKED / name).write_text("x")
        st["tasks"]["o"] = {"attempts": 1, "history": []}
        self.add("o-merge", body="merge it")
        fu = self.task("o-merge")
        fu.fields["merge-of"] = "o"
        self.assertEqual(self.X.after_integrate(fu, "review/o-merge", "merged", st, {"done.txt"}), "merged")
        self.assertEqual(st["tasks"]["o"]["merge"], "merged by o-merge")
        self.assertEqual(list(self.R.PARKED.glob("o.*")), [], "nothing left waiting on a person")
        day = self.R.ARCHIVE / self.R.now().date().isoformat()
        self.assertEqual({p.name for p in day.glob("o.*")}, {"o.md", "o.why.txt", "o.report.md"})

    def test_a_merge_teaches_the_map(self):
        a = self.worked("a-task", {"eval/cases.py": "CASES = [1]\n"})
        self.task("a-task").path.write_text(self.task("a-task").path.read_text().replace("# a-task", "# Eval cases for tools"))
        task = self.task("a-task")
        self.assertEqual(self.finish(task, a[1], a[2])[0], "PASS")
        m = self.X.load_map()
        self.assertEqual(set(m["words"]["eval"]["files"]), {"eval/cases.py"})
        self.assertIn("tools", m["words"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
