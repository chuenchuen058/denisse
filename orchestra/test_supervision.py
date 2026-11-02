"""Lane children can't outlive their timeout: the Windows Job Object in runner.spawn_lane.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_supervision -v
Each test spawns real processes (a fake lane, its grandchild `sleep 60`) and checks the
grandchild is gone afterwards. Windows only: the job object is the thing under test.
"""
# checks: slow  (~11s: spawns real process trees and waits them out)
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "hooks"))

# The fake lane: starts a grandchild `sleep 60` that writes its own pid, then copies the slot
# lock (written before it reads stdin) and sleeps past the timeout. With FAKE_ORPHAN the
# grandchild is started through an intermediate that exits at once, so the grandchild's parent
# is gone: taskkill /T walks parent pids and can't find it; only the job can.
GRANDCHILD = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
FAKE = textwrap.dedent("""
    import os, sys, time, subprocess, pathlib, shutil
    sys.stdin.read()
    lock = pathlib.Path(os.environ["FAKE_LOCK"])
    if lock.exists():
        shutil.copyfile(lock, os.environ["FAKE_LOCK_COPY"])
    gc = [sys.executable, "-c", os.environ["FAKE_GC"], os.environ["FAKE_PIDFILE"]]
    if os.environ.get("FAKE_ORPHAN"):
        mid = "import subprocess, sys; subprocess.Popen(sys.argv[1:])"
        subprocess.run([sys.executable, "-c", mid, *gc])
    else:
        subprocess.Popen(gc)
    time.sleep(60)
""")

TASK = """# supervision
Queue: auto
Priority: normal
Weight: light
Kind: work
Lane: fake
Written: 2026-11-02T20:00

Do the thing.

## Gate
```
"{py}" -c "pass"
```
"""


def wait_for_pid(path: Path, seconds: float = 20) -> int:
    end = time.time() + seconds
    while time.time() < end:
        try:
            return int(path.read_text())
        except (OSError, ValueError):
            time.sleep(0.1)
    raise AssertionError(f"the grandchild never wrote {path}")


@unittest.skipUnless(os.name == "nt", "Windows Job Objects")
class SupervisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch sup King's "))  # space + apostrophe round-trip
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        g("init", "-q", "-b", "main")
        g("config", "user.name", "t"); g("config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text(".review/*\n.venv\n")
        (self.repo / "orchestra").mkdir()
        shutil.copy(HERE / "review_prompt.md", self.repo / "orchestra" / "review_prompt.md")
        g("add", "."); g("commit", "-qm", "init")
        (self.repo / ".venv").mkdir()
        (self.repo / ".review").mkdir()
        (self.repo / ".review" / "lanes.json").write_text(json.dumps({"fake": [sys.executable, "-c", FAKE]}))
        self.pidfile = self.tmp / "grandchild.pid"
        self.lock_copy = self.tmp / "lock.json"
        self.env = {"ORCHESTRA_REPO": str(self.repo), "ORCHESTRA_BACKOFF_MIN": "0",
                    "ORCHESTRA_DAILY_SPEND_CAP_USD": "5",
                    "ORCHESTRA_TIMEOUT_S": json.dumps({"light": 3, "heavy": 6}),
                    "FAKE_GC": GRANDCHILD, "FAKE_PIDFILE": str(self.pidfile),
                    "FAKE_LOCK": str(self.repo / ".review" / "runner.lock"),
                    "FAKE_LOCK_COPY": str(self.lock_copy)}
        self.saved = {k: os.environ.get(k) for k in [*self.env, "FAKE_ORPHAN", "ORCHESTRA_AFTERCARE"]}
        os.environ.update(self.env)
        os.environ.pop("FAKE_ORPHAN", None)
        os.environ.pop("ORCHESTRA_AFTERCARE", None)  # no aftercare = no parallel slots = one runner.lock
        import runner
        self.R = importlib.reload(runner)
        self.gc_pid = None

    def tearDown(self):
        if self.gc_pid and self.R.proc_created(self.gc_pid) is not None:  # don't leak it on failure
            subprocess.run(["taskkill", "/PID", str(self.gc_pid), "/F"], capture_output=True)
        for wt in (self.R.WORKTREES.glob("*") if self.R.WORKTREES.exists() else []):
            self.R.unlink_link(wt / ".venv")
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertGone(self, pid: int, seconds: float = 10):
        end = time.time() + seconds
        while time.time() < end:
            if self.R.proc_created(pid) is None:
                return
            time.sleep(0.2)
        self.fail(f"grandchild {pid} is still running")

    def tick_with_timeout(self):
        (self.R.QUEUE).mkdir(parents=True, exist_ok=True)
        (self.R.QUEUE / "sup.md").write_text(TASK.format(py=sys.executable))
        self.assertEqual(self.R.tick(), "TIMEOUT")
        self.gc_pid = wait_for_pid(self.pidfile)
        return self.gc_pid

    def test_grandchild_dies_with_a_timed_out_lane(self):
        self.assertGone(self.tick_with_timeout())

    def test_orphaned_grandchild_dies_too(self):
        # Its parent exited before the timeout: invisible to taskkill /T, not to the job.
        os.environ["FAKE_ORPHAN"] = "1"
        self.assertGone(self.tick_with_timeout())

    def test_lock_records_lane_pid_and_job(self):
        self.tick_with_timeout()
        info = json.loads(self.lock_copy.read_text())
        self.assertTrue(info.get("lane_pid"))
        self.assertTrue(str(info.get("lane_job") or "").startswith("orchestra-lane-"), info)
        self.assertFalse(self.R._JOBS, "every job is closed once the lane is done")

    def test_runner_crash_takes_the_lane_tree_with_it(self):
        # A runner that dies without cleaning up (Task Scheduler kill, crash, the watchdog's
        # os._exit): Windows closes its job handle, and KILL_ON_JOB_CLOSE does the rest.
        crash = textwrap.dedent(f"""
            import os, subprocess, sys, time
            sys.path.insert(0, {str(HERE)!r})
            import runner
            gc = [sys.executable, "-c", {GRANDCHILD!r}, {str(self.pidfile)!r}]
            mid = "import subprocess, sys, time; subprocess.Popen(sys.argv[1:]); time.sleep(60)"
            proc, job = runner.spawn_lane([sys.executable, "-c", mid, *gc])
            assert job, "no job object"
            while not os.path.exists({str(self.pidfile)!r}):
                time.sleep(0.1)
            os._exit(9)
        """)
        r = subprocess.run([sys.executable, "-c", crash], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 9, r.stderr)
        self.gc_pid = wait_for_pid(self.pidfile)
        self.assertGone(self.gc_pid)

    def test_watchdog_close_kills_the_tree(self):
        mid = "import subprocess, sys, time; subprocess.Popen(sys.argv[1:]); time.sleep(60)"
        proc, job = self.R.spawn_lane([sys.executable, "-c", mid, sys.executable, "-c", GRANDCHILD,
                                       str(self.pidfile)])
        self.assertTrue(job)
        self.gc_pid = wait_for_pid(self.pidfile)
        self.R.close_all_jobs()
        proc.wait(timeout=10)
        self.assertGone(self.gc_pid)


if __name__ == "__main__":
    unittest.main()
