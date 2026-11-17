"""A reviewer that didn't review is not a reviewer that blocked (10 Nov 2026, 01:53-02:02).

    .venv\\Scripts\\python.exe -m unittest orchestra.test_review_retry -v

Two PASSes were parked "reviewer gave no VERDICT line" because the reviewer's answer was
"You've hit your 5-hour limit · resets 5:20am (Europe/London)". Each test builds a throwaway git
repo with a fake lane and a fake reviewer that gives scripted answers, one per run.
"""
# checks: slow  (real git repos, fake lanes, whole ticks)
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
from unittest import mock

os.environ["ORCHESTRA_LIVE_WINDOW"] = "false"
os.environ["ORCHESTRA_TOASTS"] = "false"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent))  # testing_git; appended so it never shadows a local module
import testing_git  # noqa: E402

# Fake lane: commits a change under orchestra/ (risky, so the reviewer runs), reports PASS and
# counts its runs in FAKE_LANE_RUNS.
FAKE = textwrap.dedent("""
    import os, sys, subprocess, pathlib, json
    sys.stdin.read()
    runs = pathlib.Path(os.environ["FAKE_LANE_RUNS"])
    runs.write_text(str(int(runs.read_text() or 0) + 1 if runs.exists() else 1))
    p = pathlib.Path("orchestra/notes.txt")
    p.write_text("changed\\n", encoding="utf-8")
    subprocess.run(["git", "add", str(p)], check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fake"], check=True)
    pathlib.Path(os.environ["ORCHESTRA_REPORT"]).write_text("RESULT: PASS\\nGATE: ok\\nHUMAN: none\\nPREMISE: right\\n")
    print(json.dumps({"type": "result", "total_cost_usd": 0.01}))
""")
# Fake reviewer: prints the next answer from FAKE_ANSWERS (a JSON list), and counts its runs.
FAKE_REVIEWER = textwrap.dedent("""
    import os, sys, pathlib, json
    sys.stdin.buffer.read()
    f = pathlib.Path(os.environ["FAKE_ANSWERS"])
    answers = json.loads(f.read_text(encoding="utf-8"))
    f.write_text(json.dumps(answers[1:]), encoding="utf-8")
    runs = pathlib.Path(os.environ["FAKE_REVIEW_RUNS"])
    runs.write_text(str(int(runs.read_text() or 0) + 1 if runs.exists() else 1))
    sys.stdout.buffer.write(answers[0].encode("utf-8"))
""")
LIMIT = "You've hit your 5-hour limit · resets 5:20am (Europe/London)\n"
OK = "VERDICT: ok\n- fine\n"
BLOCK = "VERDICT: block\n- orchestra/notes.txt:1 a real objection\n"
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
"{py}" -c "pass"
```
"""
ENV = ("FAKE_LANE_RUNS", "FAKE_ANSWERS", "FAKE_REVIEW_RUNS")


class ReviewRetryTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text(".review/*\n.venv\n")
        (self.repo / "orchestra").mkdir()
        shutil.copy(HERE / "review_prompt.md", self.repo / "orchestra" / "review_prompt.md")
        (self.repo / "orchestra" / "notes.txt").write_text("notes\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "init")
        (self.repo / ".venv").mkdir()
        (self.repo / ".review").mkdir()
        (self.repo / ".review" / "lanes.json").write_text(json.dumps({
            "fake": [sys.executable, "-c", FAKE], "reviewer": [sys.executable, "-c", FAKE_REVIEWER]}))
        self.lane_runs, self.review_runs = self.tmp / "lane_runs", self.tmp / "review_runs"
        self.answers = self.tmp / "answers.json"
        os.environ.update(ORCHESTRA_REPO=str(self.repo), ORCHESTRA_BACKOFF_MIN="0",
                          ORCHESTRA_DAILY_SPEND_CAP_USD="50", ORCHESTRA_POWER="ac",
                          ORCHESTRA_TIMEOUT_S=json.dumps({"light": 30, "heavy": 60}),
                          FAKE_LANE_RUNS=str(self.lane_runs), FAKE_REVIEW_RUNS=str(self.review_runs),
                          FAKE_ANSWERS=str(self.answers))
        import runner
        self.R = importlib.reload(runner)

    def tearDown(self):
        for wt in (self.R.WORKTREES.glob("*") if self.R.WORKTREES.exists() else []):
            self.R.unlink_link(wt / ".venv")
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ENV:
            os.environ.pop(k, None)

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()

    def add(self, slug, *answers):
        self.answers.write_text(json.dumps(list(answers)), encoding="utf-8")
        (self.R.QUEUE).mkdir(parents=True, exist_ok=True)
        (self.R.QUEUE / f"{slug}.md").write_text(TASK.format(
            slug=slug, py=sys.executable, written=dt.datetime.now().strftime("%Y-%m-%dT%H:%M")))

    def runs(self, f):
        return int(f.read_text()) if f.exists() else 0

    def state(self):
        return json.loads(self.R.STATE_FILE.read_text(encoding="utf-8"))

    def why(self, slug):
        return (self.R.PARKED / f"{slug}.why.txt").read_text(encoding="utf-8")

    # ---

    def test_limit_reply_waits_for_the_reset_then_reviews_without_rerunning_the_lane(self):
        self.add("lim", LIMIT, OK)
        self.assertEqual(self.R.tick(), "REVIEW-UNAVAILABLE")
        self.assertEqual(self.runs(self.review_runs), 1, "a limit is not retried straight away")
        why = self.why("lim")
        self.assertIn(self.R.C["review_unavailable_mark"], why, "the PASS stands; not a block")
        self.assertNotIn("no VERDICT line", why)
        self.assertIn("5-hour limit", why)
        st = self.state()
        self.assertEqual(st["tasks"]["lim"]["review"]["verdict"], "unavailable")
        until = dt.datetime.fromisoformat(st["lanes"]["reviewer"]["limited_until"])
        self.assertEqual((until.hour, until.minute), (5, 20), "the reset the message names")
        self.assertEqual(self.git("rev-list", "--count", "main..review/lim"), "1", "the PASSed branch is kept")

        # Before the reset: nothing is re-queued, and no attempt is used up.
        self.assertEqual(self.R.auto_requeue_review_step(), [])
        self.assertFalse((self.R.QUEUE / "lim.md").exists())
        self.assertEqual(self.runs(self.review_runs), 1)

        # After the reset: re-queued merge-only; the next tick runs only the review, which passes.
        later = until + dt.timedelta(minutes=1)
        with mock.patch.object(self.R, "now", return_value=later):
            self.assertEqual(self.R.auto_requeue_review_step(), ["lim"])
        self.assertTrue(self.R.is_merge_only(self.R.parse_task(self.R.QUEUE / "lim.md")))
        self.assertEqual(self.R.tick(), "PASS")
        self.assertEqual(self.runs(self.review_runs), 2)
        self.assertEqual(self.runs(self.lane_runs), 1, "the lane is never re-run for a review")
        self.assertEqual(self.git("show", "main:orchestra/notes.txt"), "changed", "merged into master")
        self.assertEqual(self.state()["tasks"]["lim"]["review"]["verdict"], "ok")

    def test_a_real_block_parks(self):
        # With automatic fix rounds off (auto_fix_round_max 0), a block parks as it did before
        # them; test_a_real_block_gets_a_fix_round_not_a_retry covers the default.
        self.R.C["auto_fix_round_max"] = 0
        self.add("blk", BLOCK, OK)
        self.assertEqual(self.R.tick(), "REVIEW-BLOCKED")
        self.assertEqual(self.runs(self.review_runs), 1, "a verdict is final: no retry")
        why = self.why("blk")
        self.assertIn("reviewer blocked it", why)
        self.assertNotIn(self.R.C["review_unavailable_mark"], why, "never auto re-queued")
        self.assertEqual(self.R.auto_requeue_review_step(), [])
        self.assertIn("a real objection", (self.R.PARKED / "blk.review.md").read_text(encoding="utf-8"))
        self.assertNotIn("reviewer", self.state().get("lanes", {}), "a block limits nothing")

    def test_a_real_block_gets_a_fix_round_not_a_retry(self):
        # Merged 15 Nov 2026 with the automatic fix round: a block with findings is still a
        # verdict (no immediate reviewer retry, not REVIEW-UNAVAILABLE); it re-queues for a fix.
        self.assertGreater(int(self.R.C["auto_fix_round_max"]), 0, "the default has fix rounds on")
        self.add("blk2", BLOCK, OK)
        self.assertEqual(self.R.tick(), "AUTO-FIX")
        self.assertEqual(self.runs(self.review_runs), 1, "a verdict is final: no retry")
        self.assertFalse((self.R.PARKED / "blk2.why.txt").exists(), "not parked")
        self.assertEqual(self.R.auto_requeue_review_step(), [], "not waiting as REVIEW-UNAVAILABLE")
        self.assertNotIn("reviewer", self.state().get("lanes", {}), "a block limits nothing")
        requeued = (self.R.QUEUE / "blk2.md").read_text(encoding="utf-8")
        self.assertIn("a real objection", requeued, "the findings are the fix round's note")

    def test_empty_output_is_retried_once(self):
        self.add("emp", "", OK)
        self.assertEqual(self.R.tick(), "PASS")
        self.assertEqual(self.runs(self.review_runs), 2, "one retry, then its verdict counts")
        self.assertEqual(self.git("show", "main:orchestra/notes.txt"), "changed", "merged into master")

    def test_empty_output_twice_waits_instead_of_parking_as_blocked(self):
        self.add("emp2", "", "", OK)
        self.assertEqual(self.R.tick(), "REVIEW-UNAVAILABLE")
        self.assertEqual(self.runs(self.review_runs), 1 + int(self.R.C["review_infra_retries"]), "capped")
        why = self.why("emp2")
        self.assertIn(self.R.C["review_unavailable_mark"], why)
        self.assertIn("no output", why)
        self.assertNotIn("reviewer", self.state().get("lanes", {}), "no reset to wait for")
        self.assertEqual(self.R.auto_requeue_review_step(), ["emp2"], "retried later, by the auto re-queue")


if __name__ == "__main__":
    unittest.main()
