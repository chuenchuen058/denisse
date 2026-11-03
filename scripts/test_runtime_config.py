"""runtime_config: changing config.json settings.runtime changes what
standing_instructions, conversation and runtime_status do, and a bad value
logs a line and falls back to the default instead of crashing.

Temp config files only (DENISSE_CONFIG); no tray, no Ollama.

    .venv\\Scripts\\python.exe scripts\\test_runtime_config.py
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import conversation  # noqa: E402
import runtime_config  # noqa: E402
import runtime_status  # noqa: E402
import standing_instructions as standing  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="denisse-runtime-config-"))


class RuntimeConfigTests(unittest.TestCase):
    def use(self, runtime):
        """Point settings at a temp config.json whose settings.runtime is `runtime`."""
        path = TMP / f"config-{self.id().rsplit('.', 1)[-1]}.json"
        path.write_text(json.dumps({"settings": {"runtime": runtime}}), encoding="utf-8")
        env = patch.dict(os.environ, {"DENISSE_CONFIG": str(path)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(runtime_config.reload)
        runtime_config.reload()

    def setUp(self):
        runtime_config.reload()

    # -- defaults ---------------------------------------------------------
    def test_defaults_match_the_old_hardcoded_values(self):
        jobs = {j["id"]: j["every_seconds"] for j in standing.defaults()["jobs"]}
        self.assertEqual(jobs, {"task_review": 21600, "project_review": 3600, "downloads": 15})
        self.assertEqual(runtime_status.every_seconds(), 10)
        self.assertEqual(runtime_status.stale_seconds(), 30)
        self.assertTrue(conversation.looks_like_noise("the uh the"))
        self.assertIsNotNone(conversation._deflection().search("Could you rephrase?"))

    # -- standing_instructions -------------------------------------------
    def test_job_interval_and_goal_come_from_data(self):
        dflt = runtime_config.defaults()["standing_instructions"]["jobs"]
        jobs = [dict(j) for j in dflt]
        jobs[0].update(every_seconds=7200, goal="Look at tasks.")
        self.use({"standing_instructions": {"jobs": jobs}})
        first = standing.defaults()["jobs"][0]
        self.assertEqual((first["every_seconds"], first["goal"]), (7200, "Look at tasks."))

    def test_min_interval_limit_comes_from_data(self):
        data = standing.defaults()
        data["jobs"][2]["every_seconds"] = 15
        standing.validate(data)  # 15 s downloads is allowed by default
        self.use({"standing_instructions": {"min_every_seconds": {"downloads": 60}}})
        with self.assertRaises(ValueError):
            standing.validate(data)

    def test_out_of_range_jobs_fall_back_to_shipped_jobs(self):
        jobs = [dict(j) for j in runtime_config.defaults()["standing_instructions"]["jobs"]]
        jobs[0]["every_seconds"] = 1  # below the 300 s minimum
        self.use({"standing_instructions": {"jobs": jobs}})
        with self.assertLogs("denisse.standing_instructions", "WARNING"):
            got = standing.defaults()
        self.assertEqual(got["jobs"][0]["every_seconds"], 21600)

    # -- conversation ----------------------------------------------------
    def test_noise_filler_words_come_from_data(self):
        self.assertFalse(conversation.looks_like_noise("banana"))
        self.use({"conversation": {"noise_filler_words": ["banana"]}})
        self.assertTrue(conversation.looks_like_noise("banana banana"))
        self.assertFalse(conversation.looks_like_noise("the"))

    def test_deflection_patterns_come_from_data(self):
        def turn(user, reply):
            return [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]
        groups = conversation._turns(turn("hi", "Purple monkey.") + turn("yo", "Could you rephrase?"))
        kept = [g[0]["content"] for g in conversation._drop_failed(groups)]
        self.assertEqual(kept, ["hi"])
        self.use({"conversation": {"deflection_patterns": ["purple\\s+monkey"]}})
        kept = [g[0]["content"] for g in conversation._drop_failed(groups)]
        self.assertEqual(kept, ["yo"])

    def test_bad_regex_is_dropped_with_a_log_line(self):
        with self.assertLogs("denisse.runtime_config", "WARNING") as logs:
            self.use({"conversation": {"deflection_patterns": ["(unclosed", "ok\\b"]}})
        self.assertIn("deflection_patterns[0]", "\n".join(logs.output))
        self.assertEqual(runtime_config.get("conversation")["deflection_patterns"], ["ok\\b"])

    # -- runtime_status --------------------------------------------------
    def test_stale_threshold_and_wording_come_from_data(self):
        path = str(TMP / "status.json")
        once = runtime_status.start(lambda: {}, path=path, every=3600)
        once()
        later = os.path.getmtime(path) + 60  # 60 s after the write: stale by default (30 s)
        _, blockers = runtime_status.report(path, now=later)
        self.assertTrue(any("old" in b for b in blockers), blockers)
        self.use({"runtime_status": {"write_every_seconds": 60, "stale_after_writes": 5}})
        _, blockers = runtime_status.report(path, now=later)
        self.assertEqual(blockers, [])
        self.use({"runtime_status": {"wording": {"stale": "stuck for {age:.0f} seconds"}}})
        _, blockers = runtime_status.report(path, now=later)
        self.assertTrue(any(b.startswith("stuck for") for b in blockers), blockers)

    def test_bad_values_fall_back_with_a_log_line(self):
        with self.assertLogs("denisse.runtime_config", "WARNING") as logs:
            self.use({"runtime_status": {"write_every_seconds": "ten", "read_attempts": -1,
                                         "wording": {"job": "job {nope}"}},
                      "standing_instructions": {"poll_seconds": True}})
        text = "\n".join(logs.output)
        for key in ("write_every_seconds", "read_attempts", "wording.job", "poll_seconds"):
            self.assertIn(key, text)
        cfg = runtime_config.get("runtime_status")
        self.assertEqual((cfg["write_every_seconds"], cfg["read_attempts"]), (10, 10))
        self.assertEqual(runtime_config.wording("runtime_status", "job", job="x"), "job running: x")
        self.assertEqual(runtime_config.get("standing_instructions")["poll_seconds"], 5)

    def test_template_that_wont_format_uses_default(self):
        self.use({"runtime_status": {"wording": {"exited": "pid {pid:.2f}"}}})  # pid is an int
        with self.assertLogs("denisse.runtime_config", "WARNING"):
            said = runtime_config.wording("runtime_status", "exited", pid="123")
        self.assertIn("123", said)

    def test_not_an_object_uses_defaults(self):
        with self.assertLogs("denisse.runtime_config", "WARNING"):
            self.use(["not", "an", "object"])
        self.assertEqual(runtime_config.get("runtime_status")["write_every_seconds"], 10)


if __name__ == "__main__":
    unittest.main()
