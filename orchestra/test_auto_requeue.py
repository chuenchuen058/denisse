"""Tests for the runner's auto-requeue of reviewer-unavailable PASSes (auto_requeue_review_step).

    .venv\\Scripts\\python.exe -m unittest orchestra.test_auto_requeue -v
Each test builds a throwaway git repo, so the real queue is never touched. No lane runs.
"""
import datetime as dt
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent))  # testing_git; appended so it never shadows a local module
import testing_git  # noqa: E402

os.environ["ORCHESTRA_LIVE_WINDOW"] = "false"
os.environ["ORCHESTRA_TOASTS"] = "false"

TASK = """# {slug}
Queue: auto
Priority: normal
Weight: light
Kind: work
Lane: worker
Written: 2026-11-11T20:00

Do {slug}.

## Gate
```
python -c "pass"
```
"""


class AutoRequeueTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text(".review/*\n")
        self.git("add", ".")
        self.git("commit", "-qm", "init")
        (self.repo / ".review").mkdir()
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_POWER"] = "ac"
        import runner, requeue, aftercare
        self.R = importlib.reload(runner)
        importlib.reload(requeue)
        self.A = aftercare
        self.R._BASE = None
        for d in (self.R.QUEUE, self.R.PARKED, self.R.REPORTS):
            d.mkdir(parents=True, exist_ok=True)
        self.saved = dict(self.R.C)
        self.R.C.update(auto_requeue_review_max_per_tick=2, auto_requeue_review_max_age_days=3,
                        auto_requeue_review_max_attempts=2)
        self.reviewer = mock.patch.object(self.R, "pick_reviewer",
                                          return_value={"lane": "rv", "argv": ["x"], "why": "test"})
        self.reviewer.start()
        self.addCleanup(self.reviewer.stop)

    def tearDown(self):
        self.R.C.clear()
        self.R.C.update(self.saved)
        self.R._BASE = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def park(self, slug, reason, hours_ago=1, verdict="unavailable", branch=True):
        if branch:
            self.git("branch", f"review/{slug}")
        (self.R.PARKED / f"{slug}.md").write_text(TASK.format(slug=slug), encoding="utf-8")
        (self.R.PARKED / f"{slug}.report.md").write_text("RESULT: PASS\n", encoding="utf-8")
        when = (self.R.now() - dt.timedelta(hours=hours_ago)).isoformat()
        (self.R.PARKED / f"{slug}.why.txt").write_text(f"{when} {reason}\nTo run it again: ...\n", encoding="utf-8")
        st = self.R.load_state()
        st["tasks"][slug] = {"attempts": 1, "history": [], "last_result": "PASS",
                             **({"review": {"verdict": verdict}} if verdict else {})}
        self.R.save_state(st)

    def unavailable(self, slug):
        """The park reason the runner writes for REVIEW-UNAVAILABLE, from config like the runner."""
        C = self.R.C
        return C["review_unavailable_park"].format(reasons="no reviewer lane available: codex limited",
                                                   mark=C["review_unavailable_mark"], slug=slug,
                                                   branch=f"review/{slug}")

    def log(self):
        return self.R.LOG_FILE.read_text(encoding="utf-8") if self.R.LOG_FILE.exists() else ""

    # ---

    def test_reviewer_unavailable_and_reviewer_free_is_requeued_once(self):
        self.park("a", self.unavailable("a"))
        self.assertEqual(self.R.auto_requeue_review_step(), ["a"])
        q = self.R.QUEUE / "a.md"
        self.assertTrue(q.exists())
        self.assertFalse((self.R.PARKED / "a.md").exists())
        self.assertFalse((self.R.PARKED / "a.why.txt").exists())
        task = self.R.parse_task(q)
        self.assertTrue(self.R.is_merge_only(task))
        self.assertEqual(task.fields[self.R.C["auto_requeue_review_header"].lower()], "1")
        self.assertEqual(self.R.load_state()["tasks"]["a"]["attempts"], 0)
        self.assertIn("AUTO-REQUEUE a: reviewer rv free", self.log())
        # Next tick: already queued, so nothing more happens.
        self.assertEqual(self.R.auto_requeue_review_step(), [])
        self.assertEqual(self.log().count("AUTO-REQUEUE a:"), 1)

    def test_oldest_first_and_capped_per_tick(self):
        self.R.C["auto_requeue_review_max_per_tick"] = 1
        self.park("new", self.unavailable("new"), hours_ago=1)
        self.park("old", self.unavailable("old"), hours_ago=5)
        self.assertEqual(self.R.auto_requeue_review_step(), ["old"])
        self.assertTrue((self.R.PARKED / "new.md").exists())

    def test_reviewer_blocked_is_left(self):
        self.park("b", "reviewer blocked it (- x.py:1 bad); review in b.review.md. Branch review/b kept.",
                  verdict="block")
        self.assertEqual(self.R.auto_requeue_review_step(), [])
        self.assertTrue((self.R.PARKED / "b.md").exists())

    def test_blocked_verdict_in_state_wins_over_the_mark(self):
        self.park("b2", self.unavailable("b2"), verdict="block")
        self.assertEqual(self.R.auto_requeue_review_step(), [])
        self.assertTrue((self.R.PARKED / "b2.md").exists())

    def test_conflict_and_gate_failure_are_left(self):
        self.park("c", self.R.C["merge_only_park"].format(why="merge conflict in lib.py", branch="review/c"),
                  verdict="ok")
        self.park("g", "session said PASS, but runner's own gate check: fail: exit 1. Branch review/g kept.",
                  verdict=None)
        self.assertEqual(self.R.auto_requeue_review_step(), [])
        self.assertTrue((self.R.PARKED / "c.md").exists())
        self.assertTrue((self.R.PARKED / "g.md").exists())

    def test_too_old_is_left(self):
        self.park("o", self.unavailable("o"), hours_ago=24 * 4)
        self.assertEqual(self.R.auto_requeue_review_step(), [])
        self.assertTrue((self.R.PARKED / "o.md").exists())

    def test_attempts_exhausted_is_left_and_listed(self):
        self.park("x", self.unavailable("x"))
        self.R._set_header(self.R.PARKED / "x.md", self.R.C["auto_requeue_review_header"], "2")
        self.assertEqual(self.R.auto_requeue_review_step(), [])
        self.assertTrue((self.R.PARKED / "x.md").exists())
        first = (self.R.PARKED / "x.why.txt").read_text(encoding="utf-8").splitlines()[0]
        self.assertIn("Auto-requeue stopped after 2 attempt(s)", first)
        self.assertIn("The PASS stands", first, "the original reason is kept")
        lines = self.A.waiting_lines(queue_dir=self.R.QUEUE, parked_dir=self.R.PARKED, state={},
                                     now=self.R.now())
        self.assertTrue(any("`x`" in l and "Auto-requeue stopped" in l for l in lines), lines)
        # Annotated once, not on every tick.
        self.R.auto_requeue_review_step()
        first2 = (self.R.PARKED / "x.why.txt").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first, first2)

    def test_no_reviewer_free_does_nothing(self):
        self.park("n", self.unavailable("n"))
        self.reviewer.stop()
        with mock.patch.object(self.R, "pick_reviewer", return_value={"lane": None, "argv": None, "why": "all limited"}):
            self.assertEqual(self.R.auto_requeue_review_step(), [])
        self.reviewer.start()
        self.assertTrue((self.R.PARKED / "n.md").exists())
        self.assertNotIn("Auto-Requeued", (self.R.PARKED / "n.md").read_text(encoding="utf-8"))

    def test_reviewer_slot_busy_does_nothing(self):
        self.park("s", self.unavailable("s"))
        lock = self.R.REVIEW / "runner.rv.lock"
        lock.write_text(json.dumps({"pid": os.getpid(), "task": "other"}), encoding="utf-8")
        self.assertEqual(self.R.auto_requeue_review_step(), [])
        self.assertTrue((self.R.PARKED / "s.md").exists())
        lock.unlink()
        self.assertEqual(self.R.auto_requeue_review_step(), ["s"])

    def test_missing_branch_counts_the_attempt_and_stays_parked(self):
        self.park("m", self.unavailable("m"), branch=False)
        self.assertEqual(self.R.auto_requeue_review_step(), [])
        self.assertTrue((self.R.PARKED / "m.md").exists())
        self.assertEqual(self.R.parse_task(self.R.PARKED / "m.md").fields["auto-requeued"], "1")
        self.assertIn("AUTO-REQUEUE m: not re-queued", self.log())

    def test_off_when_cap_is_zero(self):
        self.R.C["auto_requeue_review_max_per_tick"] = 0
        self.park("z", self.unavailable("z"))
        self.assertEqual(self.R.auto_requeue_review_step(), [])

    def test_counter_is_seen_when_the_header_window_is_full(self):
        """parse_task reads lines[1:20]: with 19 headers already there, the counter goes on line 20.
        It must still be counted, so the attempts limit holds (Codex review, 12 Nov 2026)."""
        self.park("h", self.unavailable("h"))
        path = self.R.PARKED / "h.md"
        lines = path.read_text(encoding="utf-8").split("\n")
        extra = [f"Extra-{chr(97 + i)}: v" for i in range(19 - 6)]  # TASK has 6 headers: 19 in all
        path.write_text("\n".join(lines[:7] + extra + lines[7:]), encoding="utf-8")
        self.assertEqual(len(self.R.parse_task(path).fields), 19)
        header = self.R.C["auto_requeue_review_header"]
        self.R._set_header(path, header, "1")
        self.assertEqual(path.read_text(encoding="utf-8").split("\n")[20], f"{header}: 1", "past the window")
        self.assertEqual(self.R._header_value(path, header), "1")
        self.R._set_header(path, header, "2")  # replaced in place, not added again
        self.assertEqual(path.read_text(encoding="utf-8").count(f"{header}:"), 1)
        self.assertEqual(self.R.auto_requeue_review_step(), [], "2 of 2 attempts used: left parked")
        self.assertTrue(path.exists())
        self.assertIn("Auto-requeue stopped after 2 attempt(s)",
                      (self.R.PARKED / "h.why.txt").read_text(encoding="utf-8"))

    def test_counter_survives_the_requeue_with_a_full_header_window(self):
        self.park("f", self.unavailable("f"))
        path = self.R.PARKED / "f.md"
        lines = path.read_text(encoding="utf-8").split("\n")
        path.write_text("\n".join(lines[:7] + [f"Extra-{chr(97 + i)}: v" for i in range(13)] + lines[7:]), encoding="utf-8")
        self.assertEqual(self.R.auto_requeue_review_step(), ["f"])
        self.assertEqual(self.R._header_value(self.R.QUEUE / "f.md", self.R.C["auto_requeue_review_header"]), "1")

    def test_runs_under_the_dispatch_lock(self):
        seen = []
        real = self.R._auto_requeue_review
        def check(state):
            seen.append((self.R.REVIEW / "dispatch.lock").exists())
            return real(state)
        self.park("l", self.unavailable("l"))
        with mock.patch.object(self.R, "_auto_requeue_review", side_effect=check):
            self.assertEqual(self.R.auto_requeue_review_step(), ["l"])
        self.assertEqual(seen, [True])
        self.assertFalse((self.R.REVIEW / "dispatch.lock").exists(), "released afterwards")

    def test_waits_for_another_tick_and_sees_what_it_claimed(self):
        """Another tick holds the dispatch lock and claims the task meanwhile: this tick's
        eligibility check runs after it lets go, so the task is neither re-queued nor counted."""
        self.park("w", self.unavailable("w"))
        me = {"pid": os.getpid(), "created": self.R.proc_created(os.getpid())}
        dispatch = self.R.REVIEW / "dispatch.lock"
        dispatch.write_text(json.dumps(me), encoding="utf-8")  # "another tick is picking"
        result = {}
        th = threading.Thread(target=lambda: result.setdefault("r", self.R.auto_requeue_review_step()))
        th.start()
        time.sleep(1.0)
        self.assertTrue(th.is_alive(), "waits for the dispatch lock")
        self.assertTrue((self.R.PARKED / "w.md").exists())
        (self.R.REVIEW / "runner.other.lock").write_text(json.dumps({**me, "task": "w"}), encoding="utf-8")
        dispatch.unlink()
        th.join(timeout=60)
        self.assertFalse(th.is_alive())
        self.assertEqual(result["r"], [])
        self.assertEqual(self.R._header_value(self.R.PARKED / "w.md", self.R.C["auto_requeue_review_header"]), "")

    def test_park_text_unchanged_for_existing_readers(self):
        text = self.unavailable("t1")
        self.assertIn("The PASS stands: after signing in, `python orchestra\\requeue.py t1 --merge-only`.", text)


if __name__ == "__main__":
    unittest.main()
