"""Tests for orchestra/requeue.py and orchestra/status.py, in a throwaway repo.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_tools -v
"""
import datetime as dt
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

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

    # --- status

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
