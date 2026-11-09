"""Tests for orchestra/requeue.py and orchestra/status.py, in a throwaway repo.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_tools -v
"""
import datetime as dt
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent))  # testing_git; appended so it never shadows a local module
import testing_git  # noqa: E402

TASK = """# Parked thing
Queue: auto
Priority: normal
Weight: light
Kind: work
Lane: claude
Written: {written}

Do the thing.

## Gate
```
python -c "pass"
```
"""


class ToolsTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)  # first: no inherited GIT_* reaches a real repo
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".review").mkdir(parents=True)
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        import runner, requeue, status
        self.R = importlib.reload(runner)
        self.Q = importlib.reload(requeue)
        self.S = importlib.reload(status)
        self.R._BASE = None
        for d in (self.R.QUEUE, self.R.PARKED, self.R.REPORTS):
            d.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.R._BASE = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def park(self, slug="p1", attempts=2):
        written = dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
        task = self.R.PARKED / f"{slug}.md"
        task.write_text(TASK.format(written=written), encoding="utf-8")
        (self.R.PARKED / f"{slug}.report.md").write_text("RESULT: PARTIAL\n", encoding="utf-8")
        (self.R.PARKED / f"{slug}.why.txt").write_text("2026-11-02T19:00:00 PARTIAL: see report\nTo run it again...\n",
                                                        encoding="utf-8")
        st = self.R.load_state()
        digest = self.R.parse_task(task).digest
        st["tasks"][slug] = {"attempts": attempts, "digest": digest, "last_result": "PARTIAL",
                             "last_end": "2026-11-02T19:00:00",
                             "history": [{"end": dt.datetime.now().isoformat(), "result": "PARTIAL"}]}
        st["tasks"]["other"] = {"attempts": 1, "history": []}
        self.R.save_state(st)
        return task

    # --- requeue

    def test_requeue_moves_files_and_resets_state(self):
        before = self.park().read_text(encoding="utf-8")
        msg = self.Q.requeue("p1", note="fixed the fixture")
        q = self.R.QUEUE / "p1.md"
        self.assertTrue(q.exists())
        text = q.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(before.rstrip("\n")))
        self.assertRegex(text.rstrip().splitlines()[-1], r"^Re-queued \d{4}-\d\d-\d\dT\d\d:\d\d: fixed the fixture$")
        self.assertFalse((self.R.PARKED / "p1.md").exists())
        self.assertFalse((self.R.PARKED / "p1.why.txt").exists())
        self.assertFalse((self.R.PARKED / "p1.report.md").exists())
        self.assertEqual((self.R.REPORTS / "p1.attempt-parked.md").read_text(encoding="utf-8"), "RESULT: PARTIAL\n")
        ts = self.R.load_state()["tasks"]["p1"]
        self.assertEqual(ts["attempts"], 0)
        self.assertIsNone(ts["last_end"])
        self.assertEqual(len(ts["history"]), 1, "history is kept")
        self.assertEqual(self.R.load_state()["tasks"]["other"]["attempts"], 1, "other tasks untouched")
        self.assertIn("runnable", msg)
        t = self.R.parse_task(q)
        self.assertIsNone(self.R.why_not_runnable(t, self.R.load_state()))

    def test_requeue_without_note_and_with_md_suffix(self):
        self.park()
        self.Q.requeue("p1.md")
        last = (self.R.QUEUE / "p1.md").read_text(encoding="utf-8").rstrip().splitlines()[-1]
        self.assertRegex(last, r"^Re-queued \S+$")

    def test_second_requeue_keeps_the_first_report(self):
        self.park()
        self.Q.requeue("p1")
        os.replace(self.R.QUEUE / "p1.md", self.R.PARKED / "p1.md")
        (self.R.PARKED / "p1.report.md").write_text("RESULT: FAIL\n", encoding="utf-8")
        self.Q.requeue("p1", note="again")
        self.assertEqual((self.R.REPORTS / "p1.attempt-parked.md").read_text(encoding="utf-8"), "RESULT: PARTIAL\n")
        self.assertEqual((self.R.REPORTS / "p1.attempt-parked-2.md").read_text(encoding="utf-8"), "RESULT: FAIL\n")

    def test_requeue_refuses_a_running_task(self):
        self.park()
        (self.R.REVIEW / "runner.claude.2.lock").write_text(json.dumps({"pid": 1, "task": "p1"}))
        with self.assertRaises(self.Q.RequeueError):
            self.Q.requeue("p1")
        self.assertTrue((self.R.PARKED / "p1.md").exists(), "nothing moved")
        self.assertEqual(self.R.load_state()["tasks"]["p1"]["attempts"], 2)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()):
            from contextlib import redirect_stderr
            with redirect_stderr(err):
                self.assertEqual(self.Q.main(["p1"]), 1)
        self.assertIn("running", err.getvalue())

    def test_requeue_refuses_missing_or_already_queued(self):
        with self.assertRaises(self.Q.RequeueError):
            self.Q.requeue("nope")
        self.park()
        (self.R.QUEUE / "p1.md").write_text("# someone else's\n")
        with self.assertRaises(self.Q.RequeueError):
            self.Q.requeue("p1")
        self.assertEqual((self.R.QUEUE / "p1.md").read_text(), "# someone else's\n")

    def test_requeue_merges_with_a_lane_runner_that_saved_meanwhile(self):
        self.park()
        # A lane runner loaded state, then this requeue ran, then the runner saved its own change.
        runner_base = self.R.load_state()
        runner_state = json.loads(json.dumps(runner_base))
        runner_state["tasks"]["other"]["attempts"] = 2
        self.Q.requeue("p1")
        self.R._BASE = runner_base
        self.R.save_state(runner_state)
        self.R._BASE = None
        st = self.R.load_state()
        self.assertEqual(st["tasks"]["p1"]["attempts"], 0, "the runner's save must not undo the reset")
        self.assertEqual(st["tasks"]["other"]["attempts"], 2)
        self.assertFalse((self.R.REVIEW / "state.lock").exists())

    # --- requeue --merge-only

    def git_repo(self, branches=("p1",)):
        """Make the temp repo a git repo with review/<slug> branches."""
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, capture_output=True, text=True, check=True)
        g("init", "-q")
        g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "root")
        for slug in branches:
            g("branch", f"review/{slug}")

    def archive(self, slug="p1", day="2026-11-01", attempts=1):
        written = dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
        d = self.R.ARCHIVE / day
        d.mkdir(parents=True, exist_ok=True)
        task = d / f"{slug}.md"
        task.write_text(TASK.format(written=written).replace("Do the thing.", f"Do the thing ({day})."),
                        encoding="utf-8")
        (d / f"{slug}.report.md").write_text("RESULT: PASS\n", encoding="utf-8")
        st = self.R.load_state()
        st["tasks"][slug] = {"attempts": attempts, "digest": self.R.parse_task(task).digest, "last_result": "PASS",
                             "last_end": "2026-11-01T19:00:00", "history": [{"end": "x", "result": "PASS"}]}
        st["done"][self.R.parse_task(task).digest] = dt.datetime.now().isoformat()
        st["tasks"].setdefault("other", {"attempts": 1, "history": []})
        self.R.save_state(st)
        return task

    def check_merge_only_task(self, text, note=None):
        t = self.R.parse_task(self.R.QUEUE / "p1.md")
        self.assertEqual(t.fields["priority"], "urgent")
        self.assertEqual(text.count("Priority:"), 1)
        para = next(l for l in text.splitlines() if l.startswith(self.Q.MERGE_MARK))
        self.assertIn("`review/p1`", para)
        self.assertTrue(self.R.is_merge_only(t), "the runner sees it as merge-only, so it runs no lane")
        if note:
            self.assertIn(note, para)
        self.assertLess(text.index(para), text.index("## Gate"), "paragraph goes before the gate")
        self.assertIn('python -c "pass"', t.gate, "gate unchanged")
        self.assertIsNone(self.R.why_not_runnable(t, self.R.load_state()))
        ts = self.R.load_state()["tasks"]["p1"]
        self.assertEqual((ts["attempts"], ts["last_end"]), (0, None))

    def test_merge_only_from_newest_archive(self):
        self.git_repo()
        self.archive(day="2026-10-30")
        newest = self.archive(day="2026-11-01", attempts=3)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.Q.main(["p1", "--merge-only", "--note", "conflict in runner.py"]), 0)
        text = (self.R.QUEUE / "p1.md").read_text(encoding="utf-8")
        self.assertIn("Do the thing (2026-11-01).", text, "newest archived copy")
        self.check_merge_only_task(text, note="conflict in runner.py")
        self.assertTrue(newest.exists(), "the archive copy is history and stays")
        self.assertTrue((newest.parent / "p1.report.md").exists())
        self.assertIn("runnable", out.getvalue())
        self.assertEqual(self.R.load_state()["tasks"]["other"]["attempts"], 1, "other tasks untouched")

    def test_merge_only_from_parked(self):
        self.git_repo()
        self.park()
        self.Q.requeue("p1", merge_only=True)
        text = (self.R.QUEUE / "p1.md").read_text(encoding="utf-8")
        self.check_merge_only_task(text)
        self.assertFalse((self.R.PARKED / "p1.md").exists())
        self.assertFalse((self.R.PARKED / "p1.why.txt").exists())
        self.assertEqual((self.R.REPORTS / "p1.attempt-parked.md").read_text(encoding="utf-8"), "RESULT: PARTIAL\n")
        self.assertFalse((self.R.REVIEW / "state.lock").exists(), "state saved through the lock, and released")

    def test_merge_only_missing_branch_exits_2(self):
        self.git_repo(branches=())
        self.park()
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(self.Q.main(["p1", "--merge-only"]), 2)
        self.assertIn("review/p1", err.getvalue())
        self.assertIn("does not exist", err.getvalue())
        self.assertFalse((self.R.QUEUE / "p1.md").exists())
        self.assertTrue((self.R.PARKED / "p1.md").exists(), "nothing moved")
        self.assertEqual(self.R.load_state()["tasks"]["p1"]["attempts"], 2)

    def test_merge_only_refuses_a_running_task(self):
        self.git_repo()
        self.archive()
        (self.R.REVIEW / "runner.codex.lock").write_text(json.dumps({"pid": 1, "task": "p1"}))
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(self.Q.main(["p1", "--merge-only"]), 1)
        self.assertIn("running", err.getvalue())
        self.assertFalse((self.R.QUEUE / "p1.md").exists())
        self.assertEqual(self.R.load_state()["tasks"]["p1"]["attempts"], 1)

    def test_merge_only_twice_is_idempotent(self):
        self.git_repo()
        self.archive()
        self.Q.requeue("p1.md", note="first", merge_only=True)
        first = (self.R.QUEUE / "p1.md").read_text(encoding="utf-8")
        st = self.R.load_state()
        st["tasks"]["p1"]["attempts"] = 1
        self.R.save_state(st)
        msg = self.Q.requeue("p1", note="second", merge_only=True)
        self.assertIn("already queued", msg)
        self.assertEqual((self.R.QUEUE / "p1.md").read_text(encoding="utf-8"), first, "file unchanged")
        self.assertEqual(first.count(self.Q.MERGE_MARK), 1)
        self.assertEqual(self.R.load_state()["tasks"]["p1"]["attempts"], 0)
        self.assertEqual(list(self.R.QUEUE.glob("*.tmp")), [])

    def test_merge_only_refuses_an_unrelated_queued_task_or_nothing_to_find(self):
        self.git_repo(branches=("p1", "nope"))
        with self.assertRaises(self.Q.RequeueError):
            self.Q.requeue("nope", merge_only=True)
        self.archive()
        (self.R.QUEUE / "p1.md").write_text("# someone else's\n")
        with self.assertRaises(self.Q.RequeueError):
            self.Q.requeue("p1", merge_only=True)
        self.assertEqual((self.R.QUEUE / "p1.md").read_text(), "# someone else's\n")

    # --- status

    def test_status_running_shows_requested_and_actual_models(self):
        (self.R.REVIEW / "runner.claude.2.lock").write_text(
            json.dumps({"pid": 7, "since": "now", "task": "busy"}), encoding="utf-8")
        (self.R.QUEUE / "busy.md").write_text(
            TASK.format(written="2026-11-02T19:45").replace("Lane: claude", "Lane: claude\nModel: fable"),
            encoding="utf-8")
        (self.R.REPORTS / "busy.lane.log").write_text(
            'profile noise\n{partial\n[]\n{"type":"assistant","model":"ignore-me"}\n'
            '{"type":"system","subtype":"init","model":"claude-fable-actual"}\n', encoding="utf-8")
        snapshot = {p: p.read_bytes() for p in self.R.REVIEW.rglob("*") if p.is_file()}
        s = self.S.collect()
        self.assertEqual(s["running"][0]["model"], "fable")
        self.assertEqual(s["running"][0]["actual_model"], "claude-fable-actual")
        self.assertIn("claude slot 2: busy since now model fable (actual claude-fable-actual)", self.S.render(s))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.S.main(["--json"]), 0)
        row = json.loads(out.getvalue())["running"][0]
        self.assertEqual((row["model"], row["actual_model"]), ("fable", "claude-fable-actual"))
        self.assertEqual(snapshot, {p: p.read_bytes() for p in self.R.REVIEW.rglob("*") if p.is_file()})

    def test_status_running_defaults_for_missing_blank_and_starting_tasks(self):
        for slot, slug in (("claude", "no-header"), ("claude.2", "blank-header"),
                          ("claude.3", "missing-task"), ("codex", "plain-log"), ("codex.2", None)):
            (self.R.REVIEW / f"runner.{slot}.lock").write_text(json.dumps({"task": slug}), encoding="utf-8")
        for slug, header in (("no-header", ""), ("blank-header", "Model:   \n")):
            (self.R.QUEUE / f"{slug}.md").write_text("# Task\n" + header, encoding="utf-8")
        (self.R.REPORTS / "plain-log.lane.log").write_text("plain Codex output\n", encoding="utf-8")
        s = self.S.collect()
        self.assertEqual(len(s["running"]), 5)
        for row in s["running"]:
            self.assertEqual((row["model"], row["actual_model"]), ("default", None))
        self.assertEqual(self.S.render(s).count("model default"), 5)
        self.assertNotIn("(actual", self.S.render(s))

    def test_status_running_keeps_requested_model_until_init_appears(self):
        (self.R.REVIEW / "runner.claude.lock").write_text(json.dumps({"task": "busy"}), encoding="utf-8")
        (self.R.QUEUE / "busy.md").write_text("# Task\nModel: fable\n", encoding="utf-8")
        row = self.S.collect()["running"][0]
        self.assertEqual((row["model"], row["actual_model"]), ("fable", None))
        log = self.R.REPORTS / "busy.lane.log"
        prefix = '{"type":"system","subtype":"init","model":'
        log.write_text(prefix, encoding="utf-8")
        self.assertIsNone(self.S.collect()["running"][0]["actual_model"])
        log.write_text(prefix + '"actual-id"}\n', encoding="utf-8")
        self.assertEqual(self.S.collect()["running"][0]["actual_model"], "actual-id")
        (self.R.QUEUE / "busy.md").unlink()
        row = self.S.collect()["running"][0]
        self.assertEqual((row["model"], row["actual_model"]), ("default", "actual-id"))

    def test_status_reports_everything(self):
        self.park()
        (self.R.REVIEW / "runner.claude.lock").write_text(
            json.dumps({"pid": 7, "since": "2026-11-02T19:45:00", "task": "busy-task"}))
        (self.R.REVIEW / "runner.codex.lock").write_text(json.dumps({"pid": 8, "since": "x"}))
        (self.R.QUEUE / "q1.md").write_text(TASK.format(written="2026-11-02T19:45").replace("auto", "draft"),
                                           encoding="utf-8")
        st = self.R.load_state()
        u = self.R.today_usage(st)
        u["cost_usd"] = 1.234
        st["tasks"]["other"]["history"] = [{"end": dt.datetime.now().isoformat(), "result": "PASS"},
                                           {"end": "2000-01-01T00:00:00", "result": "FAIL"}]
        self.R.save_state(st)
        self.R.LOG_FILE.write_text("".join(f"line {i}\n" for i in range(8)))

        s = self.S.collect()
        self.assertEqual({(r["lane"], r["slot"], r["task"]) for r in s["running"]},
                         {("claude", 1, "busy-task"), ("codex", 1, None)})
        self.assertEqual(s["queue"][0]["slug"], "q1")
        self.assertEqual(s["queue"][0]["why_not_runnable"], "Queue is not auto")
        self.assertEqual(s["parked"], [{"slug": "p1", "why": "2026-11-02T19:00:00 PARTIAL: see report"}])
        self.assertEqual(s["today"]["results"], {"PARTIAL": 1, "PASS": 1})
        self.assertEqual(s["today"]["cost_usd"], 1.23)
        self.assertEqual(s["log_tail"], [f"line {i}" for i in range(3, 8)])

        text = self.S.render(s)
        for bit in ("claude slot 1: busy-task", "codex slot 1: (starting)", "q1", "Queue is not auto",
                    "p1: 2026-11-02T19:00:00 PARTIAL", "PARTIAL 1, PASS 1", "$1.23", "line 7"):
            self.assertIn(bit, text)

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.S.main(["--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["parked"][0]["slug"], "p1")

    def test_status_on_an_empty_review_dir(self):
        shutil.rmtree(self.R.REVIEW)
        s = self.S.collect()
        self.assertEqual((s["running"], s["queue"], s["parked"], s["log_tail"]), ([], [], [], []))
        self.assertIn("nothing", self.S.render(s))
        self.assertFalse(self.R.STATE_FILE.exists(), "status only reads")


if __name__ == "__main__":
    unittest.main(verbosity=2)
