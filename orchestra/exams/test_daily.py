"""Tests for orchestra/exams/daily.py: the once-a-day exam run, with fake agents and a fake clock.

    .venv\\Scripts\\python.exe -m unittest discover -s orchestra/exams -p "test_*.py" -v
"""
import datetime as dt
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from orchestra.exams import core, daily  # noqa: E402
from orchestra.exams.core import Reply  # noqa: E402
from orchestra.exams.test_exams import good_answer  # noqa: E402

CANARY = [e for e in core.load_bank() if e.get("canary")][0]
RIGHT = good_answer(CANARY)

CFG = {"exams_daily_at": "07:10", "exams_agents": ["codex", "copilot", "local"]}
AT = dt.datetime(2026, 11, 3, 7, 12)


def fake_agents(scores_file, answers):
    """run_agent for daily.run_all: each name sits the real canary exam through core.run with a
    fake agent callable. answers: name -> reply text, Reply, or an Exception to raise."""
    def run_agent(name, timeout):
        a = answers[name]
        if isinstance(a, Exception):
            raise a
        names = a if isinstance(a, dict) else {name: a}
        agents = {n: (lambda p, c, t, r=r: r if isinstance(r, Reply) else Reply(text=r)) for n, r in names.items()}
        core.run(agents, [CANARY], scores_file=scores_file, log=lambda *_: None)
    return run_agent


class DueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.last, self.lock = self.tmp / "exams" / "last_run.json", self.tmp / "exams.lock"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def due(self, at=AT, limited=(), cfg=CFG):
        return daily.due(cfg, self.last, self.lock, at, list(limited))

    def test_due_at_the_first_tick_after_the_time(self):
        self.assertIn("before 07:10", self.due(AT.replace(hour=7, minute=9)))
        self.assertIsNone(self.due(AT.replace(hour=7, minute=10)))
        self.assertIsNone(self.due(AT.replace(hour=23, minute=59)))

    def test_once_a_day(self):
        self.assertIsNotNone(daily.claim(self.lock, self.last, AT, ["codex"]))
        self.lock.unlink()  # the run finished
        self.assertEqual(self.due(AT.replace(hour=15)), "already ran today")
        self.assertIsNone(self.due(AT + dt.timedelta(days=1)))

    def test_skips_while_a_lane_is_limited(self):
        self.assertEqual(self.due(limited=["codex"]), "lane limited: codex")
        self.assertFalse(self.last.exists())

    def test_respects_the_lock(self):
        self.lock.write_text(json.dumps({"since": (AT - dt.timedelta(minutes=5)).isoformat(), "token": "x"}))
        self.assertIn("lock held", self.due())
        self.assertIsNone(daily.claim(self.lock, self.last, AT, ["codex"]))
        # a lock older than the cap plus margin is a dead run's leftover
        self.lock.write_text(json.dumps({"since": (AT - dt.timedelta(hours=2)).isoformat(), "token": "x"}))
        self.assertIsNone(self.due())
        self.assertIsNotNone(daily.claim(self.lock, self.last, AT, ["codex"]))

    def test_claim_is_taken_once(self):
        self.assertIsNotNone(daily.claim(self.lock, self.last, AT, ["codex"]))
        self.assertIsNone(daily.claim(self.lock, self.last, AT, ["codex"]))
        self.assertEqual(json.loads(self.last.read_text())["day"], "2026-11-03")

    def test_off_without_a_time(self):
        self.assertIn("exams off", self.due(cfg={"exams_daily_at": ""}))

    def test_claude_only_when_included(self):
        cfg = {"exams_agents": ["codex", "claude"]}
        self.assertEqual(daily.agents_for(cfg), ["codex"])
        self.assertEqual(daily.agents_for({**cfg, "exams_include_claude": True}), ["codex", "claude"])
        self.assertEqual(daily.agents_for({}), ["codex", "copilot", "local"])

    def test_child_argv(self):
        argv = daily.child_argv("py", agents=["codex", "local"], scores=Path("s.json"), lock=Path("l"),
                                token="t", last_run=Path("lr"), log=Path("log"), vault=None, cap=900,
                                include_claude=False)
        self.assertEqual(argv[:3], ["py", "-m", "orchestra.exams.daily"])
        self.assertIn("codex,local", argv)
        self.assertNotIn("--vault", argv)
        self.assertNotIn("--include-claude", argv)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.scores = self.tmp / "exams" / "scores.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_scores_and_pass_rates(self):
        answers = {"codex": RIGHT, "copilot": "no idea",
                   "local": {"local:llama3.1:8b": RIGHT, "local:tiny": "ANSWER: nope"}}
        run_agent = fake_agents(self.scores, answers)
        res = daily.run_all(["codex", "copilot", "local"], run_agent, scores_file=self.scores)
        self.assertEqual(res, {"codex": "1/1", "copilot": "0/1", "local:llama3.1:8b": "1/1", "local:tiny": "0/1"})
        scores = core.load_scores(self.scores)
        self.assertEqual(set(scores["agents"]), {"codex", "copilot", "local:llama3.1:8b", "local:tiny"})
        line = daily.line_for(res, 42)
        self.assertTrue(line.startswith("Exams: codex 1/1; copilot 0/1"))

    def test_a_broken_or_absent_agent_does_not_stop_the_others(self):
        run_agent = fake_agents(self.scores, {"codex": RuntimeError("boom"),
                                                 "copilot": Reply(unavailable="Copilot CLI not found"),
                                                 "local": RIGHT})
        res = daily.run_all(["codex", "copilot", "local"], run_agent, scores_file=self.scores)
        self.assertEqual(res["codex"], "RuntimeError: boom")
        self.assertTrue(res["copilot"].startswith("skipped: Copilot CLI not found"))
        self.assertEqual(res["local"], "1/1")

    def test_total_cap(self):
        t = [0.0]
        calls = []

        def run_agent(name, left):
            calls.append((name, left))
            t[0] += 600  # each agent takes ten minutes on the fake clock

        res = daily.run_all(["codex", "copilot", "local"], run_agent, scores_file=self.scores,
                            cap=900, clock=lambda: t[0])
        self.assertEqual(calls, [("codex", 900), ("copilot", 300)])
        self.assertEqual(res["local"], "not run: cap reached")

    def test_child_logs_records_and_frees_the_lock(self):
        lock, last, log = self.tmp / "exams.lock", self.tmp / "exams" / "last_run.json", self.tmp / "runner.log"
        vault = self.tmp / "vault"
        vault.mkdir()
        token = daily.claim(lock, last, dt.datetime.now().replace(microsecond=0), ["codex"])
        run_agent = fake_agents(self.scores, {"codex": RIGHT})
        rc = daily.child_main(["--agents", "codex", "--scores", str(self.scores), "--lock", str(lock),
                               "--token", token, "--last-run", str(last), "--log", str(log),
                               "--vault", str(vault)], run_agent=run_agent)
        self.assertEqual(rc, 0)
        self.assertFalse(lock.exists())
        rec = json.loads(last.read_text())
        self.assertEqual(rec["results"], {"codex": "1/1"})
        self.assertTrue(rec["finished"])
        text = log.read_text()
        self.assertIn("EXAMS start agents=codex", text)
        self.assertIn("EXAMS done Exams: codex 1/1", text)
        notes = list((vault / "Calendar" / "Orchestra").glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("- Exams: codex 1/1", notes[0].read_text(encoding="utf-8"))

    def test_child_refuses_a_lock_that_is_not_its_own(self):
        lock = self.tmp / "exams.lock"
        lock.write_text(json.dumps({"since": dt.datetime.now().isoformat(), "token": "someone-else"}))
        ran = []
        rc = daily.child_main(["--agents", "codex", "--scores", str(self.scores), "--lock", str(lock),
                               "--token", "mine"], run_agent=lambda n, t: ran.append(n))
        self.assertEqual(rc, 1)
        self.assertEqual(ran, [])
        self.assertTrue(lock.exists())  # still the other run's

    def test_child_by_hand_respects_a_held_lock(self):
        lock = self.tmp / "exams.lock"
        lock.write_text(json.dumps({"since": dt.datetime.now().isoformat(), "token": "x"}))
        ran = []
        rc = daily.child_main(["--agents", "codex", "--scores", str(self.scores), "--lock", str(lock)],
                              run_agent=lambda n, t: ran.append(n))
        self.assertEqual((rc, ran), (1, []))


if __name__ == "__main__":
    unittest.main()
