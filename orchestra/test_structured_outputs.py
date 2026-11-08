"""Fake HTTP tests for local-team and exam structured outputs; no GPU/vault/network."""
import io
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ollama_output import schema_from_example
from orchestra.local_team import core as team
from orchestra.exams import agents, core as exams


def reject():
    return HTTPError("http://fake", 400, "Bad Request", {}, io.BytesIO(b'{"error":"format not supported"}'))


class StructuredHTTPTests(unittest.TestCase):
    def test_local_team_switch_rejection_and_lease_lifetime(self):
        role = team.load_role("scout")
        answer = {"candidates": [], "nothing_because": "No synthetic failures"}
        for mode in ("on", "off", "reject"):
            cfg = deepcopy(team.DEFAULTS)
            cfg["local_team_structured_output"] = mode != "off"
            ctx = SimpleNamespace(repo=Path("unused"), cfg=cfg, ollama="http://fake")
            bodies, lease = [], Mock()

            def send(req, **kwargs):
                self.assertEqual(lease.release.call_count, 0)
                bodies.append(json.loads(req.data))
                if mode == "reject" and len(bodies) == 1:
                    raise reject()
                return io.BytesIO(json.dumps({"message": {"content": json.dumps(answer),
                                                           "thinking": "not the answer"}}).encode())

            with self.subTest(mode=mode), patch.object(team, "ready", return_value="qwen3.5:9b"), \
                 patch.object(team.GpuLease, "for_repo", return_value=lease), \
                 patch.object(team.urllib.request, "urlopen", side_effect=send):
                self.assertEqual(team.ask(ctx, role, {"logs": []}), answer)
            lease.release.assert_called_once()
            self.assertEqual(len(bodies), 2 if mode == "reject" else 1)
            if mode != "off":
                self.assertEqual(bodies[0]["format"], role.schema)
            if mode != "on":
                self.assertNotIn("format", bodies[-1])
            self.assertTrue(all(body["think"] is False for body in bodies))
            if mode == "reject":
                self.assertEqual({k: v for k, v in bodies[0].items() if k != "format"}, bodies[1])

    def test_local_team_still_validates_and_releases_on_failure(self):
        cfg = {**team.DEFAULTS, "local_team_structured_output": False}
        ctx = SimpleNamespace(repo=Path("unused"), cfg=cfg, ollama="http://fake")
        lease = Mock()
        with patch.object(team, "ready", return_value="fake"), \
             patch.object(team.GpuLease, "for_repo", return_value=lease), \
             patch.object(team.urllib.request, "urlopen", return_value=io.BytesIO(
                 b'{"message":{"content":"{}"}}')):
            with self.assertRaises(team.BadOutput):
                team.ask(ctx, team.load_role("scout"), {})
        lease.release.assert_called_once()

    def test_json_exams_pass_answer_types_without_values_and_keep_grading(self):
        bank = [exam for exam in exams.load_bank() if exam["grader"]["type"] == "json"]
        self.assertTrue(bank)
        for exam in bank:
            for mode in ("on", "off", "reject"):
                bodies = []
                policy = agents.load()
                policy["agents"]["local_structured_output"] = mode != "off"

                def send(req, **kwargs):
                    bodies.append(json.loads(req.data))
                    if mode == "reject" and len(bodies) == 1:
                        raise reject()
                    content = json.dumps(exam["grader"]["answer"])
                    if mode != "on":
                        content = "Legacy preamble\n```json\n" + content + "\n```"
                    return io.BytesIO(json.dumps({"response": content, "thinking": "not the answer"}).encode())

                with self.subTest(exam=exam["id"], mode=mode), patch.object(agents, "load", return_value=policy), \
                     patch.object(agents.urllib.request, "urlopen", side_effect=send):
                    result = exams.run_exam(agents.local("qwen3.5:9b"), exam)
                self.assertTrue(result["ok"], result)
                self.assertEqual(len(bodies), 2 if mode == "reject" else 1)
                if mode != "off":
                    self.assertEqual(bodies[0]["format"], schema_from_example(exam["grader"]["answer"]))
                    self.assertNotIn('"const"', json.dumps(bodies[0]["format"]))
                    self.assertNotIn('"enum"', json.dumps(bodies[0]["format"]))
                if mode != "on":
                    self.assertNotIn("format", bodies[-1])
                self.assertTrue(all(body["think"] is False for body in bodies))

    def test_non_json_exam_does_not_constrain_prose(self):
        exam = {"id": "synthetic", "prompt": "What is three plus four?", "grader": {
            "type": "exact", "answer": ["7"]}}
        bodies = []

        def send(req, **kwargs):
            bodies.append(json.loads(req.data))
            return io.BytesIO(b'{"response":"ANSWER: 7"}')

        with patch.object(agents.urllib.request, "urlopen", side_effect=send):
            self.assertTrue(exams.run_exam(agents.local("fake"), exam)["ok"])
        self.assertNotIn("format", bodies[0])


if __name__ == "__main__":
    unittest.main()
