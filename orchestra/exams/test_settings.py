"""Policy edits affect execution without reloading modules or calling real agents."""
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from orchestra.exams import agents, core, daily, lane_for, settings
from orchestra.exams import __main__ as cli


class ExamSettings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.policy = settings.load()
        self.file = self.root / "rules.json"
        self.save()
        patch = mock.patch.object(settings, "DATA_FILE", self.file)
        patch.start()
        self.addCleanup(patch.stop)

    def save(self):
        self.file.write_text(json.dumps(self.policy), encoding="utf-8")

    def test_retention_failure_streak_silence_and_preference(self):
        self.policy["scores"].update(keep=2, down_after=2, silent_hours=1, fallback_agents=["fast"])
        self.save()
        scores = {"agents": {}}
        for ok in (True, False, False):
            core.record(scores, "bad", "debug", {"ok": ok})
        self.assertEqual(len(scores["agents"]["bad"]["debug"]), 2)
        self.assertIn("last 2", core.not_working(scores)["bad"])
        core.record(scores, "silent", "debug", {"ok": True}, core.now() - dt.timedelta(hours=2))
        self.assertIn("no answer", core.not_working(scores)["silent"])
        for name, seconds in (("fast", 1), ("slow", 9)):
            core.record(scores, name, "debug", {"ok": True, "seconds": seconds})
        self.assertEqual(core.best(scores, "debug")["agent"], "slow")
        self.policy["scores"]["fallback_agents"] = []
        self.save()
        self.assertEqual(core.best(scores, "debug")["agent"], "fast")

    def test_routes_categories_and_fallback_are_data(self):
        self.policy["categories"] = ["new-category"]
        self.policy["routing"] = {"default_lane": "test-lane", "kind_to_category": {"new_kind": "new-category"},
                                  "title_routes": [[r"\bwidget\b", "new-category"]]}
        self.save()
        self.assertEqual(lane_for.category_for("new-category"), "new-category")
        self.assertEqual(lane_for.category_for("new-kind"), "new-category")
        self.assertEqual(lane_for.category_for(None, "A widget"), "new-category")
        self.assertIsNone(lane_for.category_for(None, "Fix code"))
        self.assertEqual(lane_for.lane_for(None, scores_file=self.root / "none"), "test-lane")

    def test_exam_and_grader_timeouts(self):
        self.policy["limits"].update(exam_s=7, grader_s=4)
        self.save()
        agent = mock.Mock(return_value=core.Reply(text="ANSWER: yes"))
        exam = {"id": "test", "prompt": "prompt", "grader": {"type": "exact", "answer": ["yes"]}}
        self.assertTrue(core.run_exam(agent, exam)["ok"])
        self.assertEqual(agent.call_args.args[2], 7)
        exam["timeout_s"] = 8
        core.run_exam(agent, exam, timeout=9)
        self.assertEqual(agent.call_args.args[2], 9)
        with mock.patch.object(core.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            core._run_test({"grader": {"cmd": ["test.py"]}}, self.root)
        self.assertEqual(run.call_args.kwargs["timeout"], 4)

    def test_canary_count_and_timeout(self):
        self.policy["canary"].update(count=2, timeout_s=4)
        self.save()
        bank = [{"canary": True, "id": str(i)} for i in range(3)]
        with mock.patch.object(core, "load_bank", return_value=bank), \
                mock.patch.object(cli, "_agents", return_value={}), mock.patch.object(cli, "_finish"), \
                mock.patch.object(core, "run", return_value={}) as run:
            self.assertEqual(cli.main(["canary"]), 0)
        self.assertEqual(run.call_args.args[1], bank[:2])
        self.assertEqual(run.call_args.kwargs["timeout"], 4)

    def test_daily_agents_lock_age_and_run_cap(self):
        self.policy["daily"].update(agents=["custom"], cap_s=2, lock_grace_s=1)
        self.save()
        self.assertEqual(daily.agents_for({}), ["custom"])
        at = dt.datetime(2026, 1, 1)
        lock = self.root / "lock"
        lock.write_text(json.dumps({"since": at.isoformat()}), encoding="utf-8")
        self.assertTrue(daily.lock_held(lock, at + dt.timedelta(seconds=2)))
        self.assertFalse(daily.lock_held(lock, at + dt.timedelta(seconds=3)))
        run = mock.Mock()
        result = daily.run_all(["one", "two"], run, scores_file=self.root / "scores",
                               clock=mock.Mock(side_effect=[0, 0, 3]))
        run.assert_called_once_with("one", 2)
        self.assertEqual(result["two"], "not run: cap reached")

    def test_scoring_preference_does_not_enable_opt_in_agents(self):
        self.policy["scores"]["fallback_agents"] = []
        self.save()
        self.assertEqual(daily.agents_for({"exams_agents": ["claude", "codex"]}), ["codex"])
        self.assertEqual(daily.agents_for({"exams_agents": ["claude"], "exams_include_claude": True}), ["claude"])

    def test_agent_prompt_and_refusal_edits(self):
        self.policy["agents"].update(gemini_prompt="New instruction", not_served_pattern="fixture refusal")
        self.save()
        with mock.patch.object(agents, "_npm_entry", return_value="entry.js"), \
                mock.patch.object(agents.shutil, "which", return_value="node"), \
                mock.patch.object(agents, "run_proc", return_value=core.Reply()) as run:
            agents.gemini("question", self.root, 5)
        self.assertEqual(run.call_args.args[0][-1], "New instruction")
        self.assertTrue(agents._refused(core.Reply(text="fixture refusal", error="exit 1")).unavailable)
        self.assertFalse(agents._refused(core.Reply(text="quota exceeded", error="exit 1")).unavailable)

    def test_local_prompt_and_discovery_timeout(self):
        self.policy["agents"].update(local_files_prompt=" FILES={files}",
                                     local_file_template="{name}:{content}", discovery_timeout_s=7)
        self.save()
        (self.root / "sample.py").write_text("example", encoding="utf-8")
        response = mock.MagicMock()
        response.__enter__.return_value = io.StringIO('{"response": "ok"}')
        with mock.patch.object(agents.urllib.request, "urlopen", return_value=response) as request:
            self.assertEqual(agents.local("fixture-model")("question", self.root, 5).text, "ok")
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual(body["prompt"], "question FILES=sample.py:example")
        with mock.patch.object(agents.urllib.request, "urlopen", side_effect=OSError) as request:
            self.assertEqual(agents.ollama_models(), [])
        self.assertEqual(request.call_args.kwargs["timeout"], 7)

    def test_invalid_policy_is_not_silently_replaced(self):
        self.file.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            lane_for.category_for(None, "fix")


if __name__ == "__main__":
    unittest.main()
