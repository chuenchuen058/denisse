"""A slot lock whose owner died is the only record of that run: taking the slot must not delete it.

10 Nov 2026 (the short soak, inside the runner's gate): a tick reconciled, then, looking for a free
slot, found a lock whose owner had been killed a moment before. take_lock() removed it on the
assumption that reconcile() had already collected its task. It had not: the task's PASS was merged
and saved but never archived, so it sat in the queue ("this exact task already PASSed") with its
worktree for good. With slightly different timing the same deletion threw a finished, unmerged
lane away and ran it again from scratch.

    .venv\\Scripts\\python.exe orchestra\\test_dead_lock_task.py
"""
import datetime as dt
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent))  # testing_git; appended so it never shadows a local module
import testing_git  # noqa: E402

TASK = """# t
Queue: auto
Priority: normal
Weight: light
Kind: work
Lane: claude
Written: {written}

Do it.

## Gate
```
"{python}" -c "pass"
```
"""


class DeadLockTaskTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        testing_git.init_repo(self.repo)
        (self.repo / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.txt"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / ".review" / "queue").mkdir(parents=True)
        self._env = {k: os.environ.get(k) for k in ("ORCHESTRA_REPO", "ORCHESTRA_POWER")}
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_POWER"] = "ac"
        import runner
        self.R = importlib.reload(runner)
        quiet = mock.patch.multiple(self.R.visible, SPAWN=mock.Mock(return_value=0), RUN=mock.Mock())
        quiet.start()
        self.addCleanup(quiet.stop)

    def tearDown(self):
        self.R.close_all_jobs()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dead_pid(self) -> int:
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def lock(self, **info) -> dict:
        info = {"pid": self.dead_pid(), "created": 12345, "since": dt.datetime.now().isoformat(), **info}
        self.R.LOCK_FILE.write_text(json.dumps(info), encoding="utf-8")
        return info

    def log(self) -> str:
        return self.R.LOG_FILE.read_text(encoding="utf-8") if self.R.LOG_FILE.exists() else ""

    def test_a_dead_lock_naming_a_task_is_left_for_reconcile(self):
        info = self.lock(task="finished", lane="claude", run="1@x")
        self.assertFalse(self.R.take_lock(self.R.load_state()), "the slot is not free: its run is not collected")
        self.assertEqual(self.R.read_lock(self.R.LOCK_FILE), info, "the lock must be exactly as its owner left it")
        line = self.log().strip().splitlines()[-1]
        self.assertIn(str(info["pid"]), line)
        self.assertIn("finished", line)
        self.assertIn(self.R.LOCK_FILE.name, line)

    def test_a_dead_lock_naming_no_task_is_still_reclaimed_at_once(self):
        self.lock()
        self.assertTrue(self.R.take_lock(self.R.load_state()))
        self.assertEqual(self.R.read_lock(self.R.LOCK_FILE)["pid"], os.getpid())
        self.R.release_lock()

    def test_the_next_reconcile_collects_what_the_lock_names(self):
        """The case from the soak: PASS merged and saved, the owner killed before archiving, and
        the lock first met by take_lock(). The task must end archived, not queued for good."""
        R = self.R
        task_file = R.QUEUE / "merged.md"
        task_file.write_text(TASK.format(written=dt.datetime.now().strftime("%Y-%m-%dT%H:%M"),
                                         python=sys.executable), encoding="utf-8")
        task = R.parse_task(task_file)
        state = R.load_state()
        ts = R.task_state(state, task)
        ts["attempts"] = 1
        ts["history"].append({"result": "PASS", "run": "1@x", "end": dt.datetime.now().isoformat()})
        ts.update(last_result="PASS", merge="merged")
        state["done"][task.digest] = dt.datetime.now().isoformat()
        R.save_state(state)
        R.REPORTS.mkdir(parents=True, exist_ok=True)
        (R.REPORTS / "merged.md").write_text("RESULT: PASS\n", encoding="utf-8")
        self.lock(task="merged", lane="claude", run="1@x")

        self.assertFalse(R.take_lock(R.load_state()))           # a tick looking for a slot: hands off
        self.assertTrue(task_file.exists())
        self.assertEqual(R.why_not_runnable(task, R.load_state())[:29], "this exact task already PASSe")
        out = R.reconcile(R.load_state())                       # the next tick
        self.assertEqual(out.get("merged"), "already collected (archived)")
        self.assertFalse(task_file.exists(), "still in the queue: it would sit there for good")
        self.assertTrue(list(R.ARCHIVE.glob("*/merged.md")))
        self.assertFalse(R.LOCK_FILE.exists())

    def test_the_log_line_is_in_both_config_files(self):
        for name in ("config.json", "config_defaults.json"):
            cfg = json.loads((HERE / name).read_text(encoding="utf-8"))
            self.assertIn("{task}", cfg["dead_lock_task_log"], name)


if __name__ == "__main__":
    unittest.main()
