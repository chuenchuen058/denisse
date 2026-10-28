"""Synthetic acceptance tests: bounded agents, persistent jobs and real file effects."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_loop
import agent_tools
import standing_instructions as standing


def decision(tool, **kwargs):
    return {"tool": tool, "note": "Check the observed result", "focus_ids": [], **kwargs}


class AgentTests(unittest.TestCase):
    def run_agent(self, decisions, tool=None, active=lambda: True):
        self.tool = tool or Mock(return_value={"tasks": [{"id": "a", "text": "Synthetic task"}]})
        model = Mock(side_effect=decisions)
        return agent_loop.run("Review tasks", {"task_snapshot": self.tool},
                              {"task_snapshot"}, model, active=active, max_steps=4)

    def test_observe_then_finish_has_verified_evidence(self):
        result = self.run_agent([decision("task_snapshot"), decision("finish", focus_ids=["a"])])
        self.assertEqual(result["status"], "completed")
        self.assertIn("task_snapshot", result["evidence"])
        self.tool.assert_called_once_with()

    def test_completed_observations_are_removed_from_next_decision(self):
        offered = []
        def model(messages, names):
            offered.append(names)
            return decision("task_snapshot") if len(offered) == 1 else decision("finish", focus_ids=["a"])
        result = self.run_agent(model)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(offered, [("task_snapshot",), ("finish",)])

    def test_finish_without_observation_is_not_completed(self):
        result = self.run_agent([decision("finish")] * 4)
        self.assertEqual(result["status"], "failed")
        self.tool.assert_not_called()

    def test_unknown_tool_does_not_execute(self):
        result = self.run_agent([decision("run_shell_command")])
        self.assertEqual(result["status"], "failed")
        self.tool.assert_not_called()

    def test_model_cannot_supply_arbitrary_arguments(self):
        result = self.run_agent([decision("task_snapshot", arguments={"path": "private"})])
        self.assertEqual(result["status"], "failed")
        self.tool.assert_not_called()

    def test_invented_task_ids_fail_verification(self):
        result = self.run_agent([decision("task_snapshot")] + [decision("finish", focus_ids=["invented"])] * 3)
        self.assertEqual(result["status"], "failed")

    def test_failure_cannot_be_claimed_as_success(self):
        result = self.run_agent([decision("task_snapshot")], Mock(side_effect=OSError("Unavailable")))
        self.assertEqual(result["status"], "failed")
        self.assertIn("Unavailable", result["error"])

    def test_loop_is_bounded_and_reads_are_cached(self):
        result = self.run_agent([decision("task_snapshot")] * 4)
        self.assertEqual(result["status"], "failed")
        self.tool.assert_called_once_with()

    def test_pause_during_model_call_prevents_tool(self):
        enabled = [True]
        def decide(*args):
            enabled[0] = False
            return decision("task_snapshot")
        result = self.run_agent(decide, active=lambda: enabled[0])
        self.assertEqual(result["status"], "paused")
        self.tool.assert_not_called()

    def test_invalid_model_output_is_a_failure(self):
        for value in (None, [], {"tool": "finish", "note": 7}, {"tool": "finish", "focus_ids": "a"}):
            with self.subTest(value=value):
                result = self.run_agent([value])
                self.assertEqual(result["status"], "failed")

    def test_local_decoder_only_offers_observed_ids(self):
        from types import SimpleNamespace
        for tasks in ([], [{"id": "actual", "text": "Synthetic"}]):
            with self.subTest(tasks=tasks), patch("ollama.Client") as client:
                client.return_value.chat.return_value = SimpleNamespace(message=SimpleNamespace(
                    content=json.dumps(decision("finish"))))
                agent_loop.LocalModel()([
                    {"role": "user", "content": "Observation / validation: " + json.dumps({"tasks": tasks})}
                ], ("finish",))
                items = client.return_value.chat.call_args.kwargs["format"]["properties"]["focus_ids"]
                self.assertEqual(items["maxItems"], len(tasks))
                if tasks:
                    self.assertEqual(items["items"]["enum"], ["actual"])


class InstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "instructions.json"
        self.state = self.root / "state.json"
        self.reports = self.root / "reports"
        self.settings = standing.defaults()
        self.settings["enabled"] = True
        self.settings["project_dir"] = str(self.root)
        self.write_config()

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self):
        self.config.write_text(json.dumps(self.settings), encoding="utf-8")

    def engine(self, model=None):
        return standing.Engine(self.config, self.state, self.reports, model=model)

    def test_invalid_configuration_fails_closed(self):
        for change in ({"enabled": "yes"}, {"shell": True}, {"project_dir": 2}):
            with self.subTest(change=change):
                self.config.write_text(json.dumps({**self.settings, **change}))
                self.assertFalse(self.engine().status()["enabled"])

    def test_unknown_or_duplicate_jobs_are_rejected(self):
        for jobs in ([{**self.settings["jobs"][0], "id": "shell"}], [self.settings["jobs"][0]] * 2):
            self.settings["jobs"] = jobs
            self.write_config()
            self.assertFalse(self.engine().status()["enabled"])

    def test_intervals_are_bounded_and_bool_is_not_an_integer(self):
        for value in (0, True, 9999999):
            self.settings["jobs"][0]["every_seconds"] = value
            self.write_config()
            self.assertFalse(self.engine().status()["enabled"])

    def test_missing_file_does_not_implicitly_enable_jobs(self):
        self.config.unlink()
        self.assertFalse(self.engine().status()["enabled"])

    def test_claim_survives_restart_and_prevents_immediate_repeat(self):
        with patch.object(agent_tools, "task_snapshot", return_value={"tasks": []}):
            first = self.engine(Mock(side_effect=[decision("task_snapshot"), decision("finish")]))
            self.assertEqual(first.run_job("task_review", now=1000)["status"], "completed")
            second = self.engine(Mock())
            self.assertIsNone(second.run_job("task_review", now=1001))
            second.model.assert_not_called()

    def test_failed_model_run_still_has_cooldown(self):
        engine = self.engine(Mock(side_effect=RuntimeError("offline")))
        result = engine.run_job("task_review", now=1000)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(self.engine(Mock()).run_job("task_review", now=1001))

    def test_pause_is_persistent(self):
        engine = self.engine(Mock())
        engine.set_enabled(False)
        self.assertFalse(self.engine().status()["enabled"])
        self.assertIsNone(engine.run_job("task_review", now=1000))

    def test_concurrent_same_job_is_skipped(self):
        engine = self.engine(Mock())
        with standing.file_lock(self.reports / "locks" / "task_review.lock") as held:
            self.assertTrue(held)
            self.assertIsNone(engine.run_job("task_review", now=1000))
        engine.model.assert_not_called()

    def test_unchanged_reviews_do_not_notify_repeatedly(self):
        def model(*args):
            return decision("task_snapshot") if len(args[0]) == 2 else decision("finish")
        with patch.object(agent_tools, "task_snapshot", return_value={"tasks": []}):
            engine = self.engine(model)
            first = engine.run_job("task_review", now=1000)
            second = engine.run_job("task_review", now=100000)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertTrue(list(self.reports.glob("*.json")))

    def test_corrupt_state_does_not_repeat_mutating_work(self):
        self.state.write_text("broken")
        with patch.object(agent_tools, "organize_downloads") as organize:
            self.assertIsNone(self.engine().run_job("downloads", now=1000))
            organize.assert_not_called()

    def test_downloads_runs_without_a_model(self):
        model = Mock(side_effect=RuntimeError("Ollama down"))
        with patch.object(agent_tools, "organize_downloads", return_value={"moved": 0, "moves": []}):
            outcome = self.engine(model).run_job("downloads", now=1000)
        self.assertEqual(outcome["status"], "completed")
        model.assert_not_called()

    def test_downloads_failure_is_recorded_and_not_immediately_repeated(self):
        engine = self.engine()
        with patch.object(agent_tools, "organize_downloads", side_effect=OSError("Folder unavailable")) as tool:
            outcome = engine.run_job("downloads", now=1000)
            self.assertEqual(outcome["status"], "failed")
            self.assertIsNone(engine.run_job("downloads", now=1001))
            tool.assert_called_once()
        state = json.loads(self.state.read_text())
        self.assertEqual(state["downloads"]["status"], "failed")
        self.assertTrue((self.reports / "downloads-latest.json").exists())

    def test_instruction_edit_during_planning_prevents_the_next_action(self):
        engine = self.engine()
        def model(*args):
            self.settings["jobs"][0]["goal"] = "A different goal"
            self.write_config()
            return decision("task_snapshot")
        engine.model = model
        with patch.object(agent_tools, "task_snapshot") as tool:
            outcome = engine.run_job("task_review", now=1000)
            tool.assert_not_called()
        self.assertEqual(outcome["status"], "paused")

    def test_project_job_only_receives_its_project_tool(self):
        seen = []
        def model(messages, names):
            seen.append(names)
            return decision("project_snapshot") if len(seen) == 1 else decision("finish")
        snapshot = {"project": "fixture", "branch": "main", "changes": "", "latest_commit": "abc", "limits": "No tests run"}
        with patch.object(agent_tools, "project_snapshot", return_value=snapshot) as tool, \
             patch.object(agent_tools, "task_snapshot") as tasks:
            outcome = self.engine(model).run_job("project_review", now=1000)
            tool.assert_called_once_with(self.root)
            tasks.assert_not_called()
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(seen, [("project_snapshot",), ("finish",)])

    def test_tray_starts_one_download_worker_and_one_review_worker(self):
        import tray
        engine = self.engine()
        threads = []
        class FakeThread:
            def __init__(self, **kwargs):
                self.args = kwargs["args"]
                self.started = False
                threads.append(self)
            def start(self):
                self.started = True
            def is_alive(self):
                return self.started
        with patch.object(tray, "_agent_engine", engine), patch.object(tray, "_agent_threads", []), \
             patch.object(tray.threading, "Thread", FakeThread):
            tray._start_agent_workers()
            tray._start_agent_workers()
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0].args[0], ("downloads",))
        self.assertEqual(threads[1].args[0], ("task_review", "project_review"))

    def test_tray_pause_and_download_toggle_persist_separately(self):
        import tray
        engine = self.engine()
        with patch.object(tray, "_agent_engine", engine):
            self.assertTrue(tray._is_organizing())
            tray._stop_organizing()
            self.assertTrue(engine.status()["enabled"])
            self.assertFalse(tray._is_organizing())
            tray._start_organizing()
            tray._toggle_independent_work()
            self.assertFalse(tray._is_organizing())
        self.assertFalse(self.engine().status()["enabled"])

    def test_cli_initialization_does_not_overwrite_existing_instructions(self):
        from scripts import standing as cli
        before = self.config.read_bytes()
        with patch.object(cli.standing, "Engine", return_value=self.engine()), \
             patch.object(sys, "argv", ["standing.py", "--init"]), patch("builtins.print"):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(before, self.config.read_bytes())


class ToolTests(unittest.TestCase):
    def test_user_briefs_use_observations_instead_of_invented_model_claims(self):
        result = {"status": "completed", "focus_ids": ["a"], "suggestion": "All tasks completed. Dirty Git tree.",
                  "evidence": {"task_snapshot": {"open_count": 1, "tasks": [
                      {"id": "a", "text": "Synthetic report", "due": None}]}}}
        brief = agent_tools.render_brief("task_review", result)
        self.assertIn("Synthetic report", brief)
        self.assertNotIn("All tasks completed", brief)
        self.assertNotIn("Dirty Git tree", brief)
        result["evidence"] = {"project_snapshot": {"project": "fixture", "branch": "main",
            "latest_commit": "abc", "changes": " M sample.py", "limits": "No tests run"}}
        result["suggestion"] = "No further action needed; all tests passed."
        brief = agent_tools.render_brief("project_review", result)
        self.assertIn("review the listed changes", brief)
        self.assertNotIn("all tests passed", brief)

    def test_task_snapshot_does_not_sync_or_complete_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tasks.json"
            path.write_text(json.dumps([{"id": "a", "text": "Synthetic", "due": None}]))
            with patch("tasks.TASKS_FILE", str(path)), patch("tasks.pull_vault_ticks") as sync:
                snapshot = agent_tools.task_snapshot()
        self.assertEqual(snapshot["tasks"][0]["id"], "a")
        sync.assert_not_called()

    def test_project_snapshot_is_read_only_in_a_real_temporary_repo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "sample.txt").write_text("example")
            before = (root / "sample.txt").read_bytes()
            result = agent_tools.project_snapshot(root)
            self.assertIn("sample.txt", result["changes"])
            self.assertEqual(before, (root / "sample.txt").read_bytes())

    def test_project_snapshot_rejects_non_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                agent_tools.project_snapshot(Path(folder))

    def test_downloads_preserves_collisions_subfolders_and_partial_files(self):
        import auto_organize
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Documents").mkdir()
            (root / "Documents" / "sample.txt").write_text("original")
            (root / "sample.txt").write_text("new")
            (root / "partial.part").write_text("unfinished")
            os.utime(root / "sample.txt", (time.time() - 120, time.time() - 120))
            with patch.object(auto_organize, "WATCH_FOLDERS", [folder]), \
                 patch.object(auto_organize, "LOG_FILE", str(root / "moves.json")):
                result = agent_tools.organize_downloads()
            self.assertEqual(result["moved"], 1)
            self.assertEqual((root / "Documents" / "sample.txt").read_text(), "original")
            self.assertEqual((root / "Documents" / "sample (2).txt").read_text(), "new")
            self.assertTrue((root / "partial.part").exists())

    def test_downloads_skips_destination_that_resolves_outside_watch_folder(self):
        import auto_organize
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "sample.txt").write_text("new")
            os.utime(root / "sample.txt", (time.time() - 120, time.time() - 120))
            realpath = os.path.realpath
            def resolve(path):
                return str(root.parent / "elsewhere") if Path(path).name == "Documents" else realpath(path)
            with patch.object(auto_organize.os.path, "realpath", side_effect=resolve), \
                 patch.object(auto_organize, "LOG_FILE", str(root / "moves.json")):
                self.assertEqual(auto_organize.organize_folder(folder), [])
            self.assertTrue((root / "sample.txt").exists())


if __name__ == "__main__":
    unittest.main()
