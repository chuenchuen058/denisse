"""runner.write_state_now(): when the in-process STATE NOW write fails, it runs state_now.py once in a
fresh process, and logs a failure only if that fails too.

    .venv\\Scripts\\python.exe orchestra\\test_state_now_fresh.py
The bug (runner.log 10 Nov 2026 06:48, 06:56): a tick that merged a change to runner.py and
state_now.py still held its old runner module, imported the new state_now.py, and failed on
"module 'runner' has no attribute 'merge_waiting'". Temp ORCHESTRA_REPO; one real child process.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="state_now_fresh_"))
os.environ["ORCHESTRA_REPO"] = str(TMP)  # before runner is imported: REVIEW is fixed at import
os.environ.setdefault("ORCHESTRA_POWER", "ac")
os.environ["ORCHESTRA_LIVE_WINDOW"] = "false"
os.environ["ORCHESTRA_TOASTS"] = "false"
sys.path.insert(0, str(HERE))
import runner  # noqa: E402
import state_now  # noqa: E402


def stale_write():
    """What the stale tick hit: the new state_now.py asking the old runner for a new attribute."""
    return runner.merge_waiting_that_this_module_lacks()


class FreshProcessTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(runner.REVIEW, ignore_errors=True)
        runner.REVIEW.mkdir(parents=True)
        self.logged = []
        self._log = mock.patch.object(runner, "log", self.logged.append)
        self._log.start()
        # In a full discover run `runner` may already be imported (and reloaded) by another test
        # module with its own temp repo; the fresh child must write where this runner looks.
        self._env = mock.patch.dict(os.environ, {"ORCHESTRA_REPO": str(runner.REPO)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def tearDown(self):
        self._log.stop()

    def test_stale_module_falls_back_to_a_fresh_process(self):
        with mock.patch.object(state_now, "write", stale_write):
            runner.write_state_now()
        self.assertTrue((runner.REVIEW / "STATE_NOW.md").is_file(), self.logged)
        self.assertEqual([l for l in self.logged if "state_now failed" in l], [])
        fresh_log = (runner.REVIEW / runner.C["state_now_fresh_log"]).read_text(encoding="utf-8")
        self.assertIn("(exit 0)", fresh_log)

    def test_in_process_success_starts_no_process(self):
        with mock.patch.object(state_now, "write", lambda: "ok"), \
                mock.patch.object(runner, "run_bounded") as rb:
            runner.write_state_now()
        rb.assert_not_called()
        self.assertEqual(self.logged, [])

    def test_both_failing_logs_once_with_both_reasons(self):
        with mock.patch.object(state_now, "write", stale_write), \
                mock.patch.object(runner, "run_bounded", return_value=(1, "Traceback\nKeyError: 'x'\n")):
            runner.write_state_now()
        self.assertEqual(len(self.logged), 1, self.logged)
        self.assertIn("state_now failed", self.logged[0])
        self.assertIn("merge_waiting_that_this_module_lacks", self.logged[0])
        self.assertIn("exit 1: KeyError: 'x'", self.logged[0])

    def test_fresh_process_timeout_is_logged(self):
        with mock.patch.object(state_now, "write", stale_write), \
                mock.patch.object(runner, "run_bounded", return_value=(None, "")) as rb:
            runner.write_state_now()
        self.assertEqual(rb.call_args.args[3], runner.C["state_now_fresh_timeout_s"])
        self.assertEqual(len(self.logged), 1, self.logged)
        self.assertIn("timed out", self.logged[0])

    def test_fresh_process_that_cannot_start_never_raises(self):
        with mock.patch.object(state_now, "write", stale_write), \
                mock.patch.object(runner, "run_bounded", side_effect=OSError("no python")):
            runner.write_state_now()
        self.assertEqual(len(self.logged), 1, self.logged)
        self.assertIn("no python", self.logged[0])


class ConfigTest(unittest.TestCase):
    def test_keys_in_config_and_defaults(self):
        for name in ("config.json", "config_defaults.json"):
            data = json.loads((HERE / name).read_text(encoding="utf-8"))
            for key in ("state_now_fresh_timeout_s", "state_now_fresh_log"):
                self.assertIn(key, data, f"{key} missing from {name}")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
