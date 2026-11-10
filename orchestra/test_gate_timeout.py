"""Timeouts that hold (audit 10 Nov 2026, O2 and O3): a gate or run_checks --fast whose child
outlives the timeout is killed with its whole tree, not waited on; the computed lane timeout has
a ceiling; and the scheduled task's own limit still outlasts the runner's.

    .venv\\Scripts\\python.exe orchestra\\test_gate_timeout.py
"""
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent))  # testing_git; appended so it never shadows a local module
import testing_git  # noqa: E402

TASK = textwrap.dedent("""\
    # t
    Queue: auto
    Priority: normal
    Weight: light
    Kind: work
    Lane: claude
    Written: 2026-11-10T00:00

    Do it.

    ## Gate
    ```
    {gate}
    ```
    """)

TIMEOUT = 2      # the gate timeout these tests set
SLEEP = 60       # how long the hung grandchild would sleep: far past TIMEOUT
SLACK = 10       # "within a few seconds of the timeout" (the old code took the child's full SLEEP)


def py(code: str) -> str:
    """A shell command running `code` in this interpreter (no double quotes inside `code`)."""
    return f'"{sys.executable}" -c "{code}"'


def hung_child(pidfile: Path) -> str:
    """A command that prints, starts a grandchild sleeping SLEEP s on the same stdout (so a pipe
    reader would wait for it), writes the grandchild's pid, and waits for it."""
    return (f"import subprocess,sys; print('gate started', flush=True); "
            f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep({SLEEP})']); "
            f"open(r'{pidfile}','w').write(str(p.pid)); p.wait()")


class GateTimeoutTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".review").mkdir(parents=True)
        self._env = {k: os.environ.get(k) for k in ("ORCHESTRA_REPO", "ORCHESTRA_POWER")}
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_POWER"] = "ac"
        import runner
        self.R = importlib.reload(runner)
        quiet = mock.patch.multiple(self.R.visible, SPAWN=mock.Mock(return_value=0), RUN=mock.Mock())
        quiet.start()
        self.addCleanup(quiet.stop)
        self.shipped = dict(self.R.C)
        self.R.C["gate_timeout_s"] = TIMEOUT
        self.R.C["process_exit_wait_timeout_s"] = 5
        self.pidfile = self.tmp / "child.pid"

    def tearDown(self):
        self.R.close_all_jobs()
        self.R.C.clear()
        self.R.C.update(self.shipped)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def task(self, gate: str):
        p = self.tmp / "t.md"
        p.write_text(TASK.format(gate=gate), encoding="utf-8")
        return self.R.parse_task(p)

    def gate_log(self, task) -> str:
        return (self.R.REPORTS / f"{task.slug}.gate.log").read_text(encoding="utf-8")

    def child_pid(self) -> int:
        self.assertTrue(self.pidfile.exists(), "the gate never started its child")
        return int(self.pidfile.read_text())

    def assert_gone(self, pid: int):
        deadline = time.time() + 5
        while self.R.proc_created(pid) is not None and time.time() < deadline:
            time.sleep(0.1)
        self.assertIsNone(self.R.proc_created(pid), f"child {pid} outlived the timeout")

    # ---------------------------------------------------------------- run_gate

    def test_hung_gate_is_broken_within_seconds_and_its_child_is_gone(self):
        t = self.task(py(hung_child(self.pidfile)))
        start = time.time()
        verdict, detail = self.R.run_gate(t, self.tmp)
        took = time.time() - start
        self.assertEqual(verdict, "broken")
        self.assertIn(f"timed out after {TIMEOUT}s", detail)
        self.assertLess(took, TIMEOUT + SLACK, f"run_gate took {took:.1f}s on a {TIMEOUT}s timeout")
        self.assert_gone(self.child_pid())
        log = self.gate_log(t)
        self.assertIn("gate started", log, "the timed-out command's partial output is in the gate log")
        self.assertIn("timed out", log)

    def test_passing_gate_is_unchanged(self):
        t = self.task(py("print('ok')") + "\n" + py("print('ok again')"))
        self.assertEqual(self.R.run_gate(t, self.tmp), ("pass", "2 command(s) exited 0"))
        log = self.gate_log(t)
        self.assertIn("ok again", log)
        self.assertIn("(exit 0)", log)

    def test_failing_gate_keeps_its_tail_and_log_name(self):
        cmd = py("import sys; print('line one'); print('line two'); print('FAILED: 1 of 9'); sys.exit(3)")
        t = self.task(cmd)
        verdict, detail = self.R.run_gate(t, self.tmp)
        self.assertEqual(verdict, "fail")
        self.assertEqual(detail, f"`{cmd}` exited 3: line one | line two | FAILED: 1 of 9 "
                                 f"(full output: {t.slug}.gate.log)")
        self.assertIn("(exit 3)", self.gate_log(t))

    def test_failing_gate_stops_at_the_first_failure(self):
        t = self.task(py("import sys; sys.exit(1)") + "\n" + py("open(r'%s','w')" % (self.tmp / "ran")))
        self.assertEqual(self.R.run_gate(t, self.tmp)[0], "fail")
        self.assertFalse((self.tmp / "ran").exists())

    def test_stderr_goes_to_the_log_too(self):
        t = self.task(py("import sys; sys.stderr.write('to stderr\\n'); sys.exit(2)"))
        verdict, detail = self.R.run_gate(t, self.tmp)
        self.assertEqual(verdict, "fail")
        self.assertIn("to stderr", detail)
        self.assertIn("to stderr", self.gate_log(t))

    @unittest.skipUnless(os.name == "nt", "kill-on-close jobs are Windows")
    def test_watchdog_takes_a_running_gate_with_it(self):
        """start_watchdog's close_all_jobs() must reach the gate's job, not only lane jobs."""
        self.R.C["gate_timeout_s"] = SLEEP * 2  # only the watchdog can end this one
        t = self.task(py(hung_child(self.pidfile)))
        out = {}
        th = threading.Thread(target=lambda: out.update(r=self.R.run_gate(t, self.tmp)), daemon=True)
        th.start()
        deadline = time.time() + 20
        while not self.pidfile.exists() and time.time() < deadline:
            time.sleep(0.1)
        pid = self.child_pid()
        self.assertTrue(self.R._JOBS, "the running gate has a job the watchdog can close")
        self.R.close_all_jobs()  # what the watchdog does before os._exit
        th.join(SLACK)
        self.assertFalse(th.is_alive(), "run_gate still waiting after its job was closed")
        self.assert_gone(pid)

    # ---------------------------------------------------------------- fast_checks

    def fast_tree(self, body: str) -> Path:
        wt = self.tmp / "wt"
        (wt / "scripts").mkdir(parents=True, exist_ok=True)
        (wt / "scripts" / "run_checks.py").write_text("# accepts --fast\n" + body + "\n", encoding="utf-8")
        return wt

    def test_fast_checks_hung_is_broken_within_seconds_and_logged(self):
        wt = self.fast_tree(hung_child(self.pidfile))
        start = time.time()
        verdict, detail = self.R.fast_checks(wt, "x")
        took = time.time() - start
        self.assertEqual((verdict, detail), ("broken", f"run_checks.py --fast timed out after {TIMEOUT}s"))
        self.assertLess(took, TIMEOUT + SLACK)
        self.assert_gone(self.child_pid())
        self.assertIn("gate started", (self.R.REPORTS / "x.fast.log").read_text(encoding="utf-8"))

    def test_fast_checks_pass_and_fail_as_before_with_a_log(self):
        wt = self.fast_tree("print('all good')")
        self.assertEqual(self.R.fast_checks(wt, "x"), ("pass", "run_checks.py --fast exited 0"))
        self.assertIn("all good", (self.R.REPORTS / "x.fast.log").read_text(encoding="utf-8"))
        wt = self.fast_tree("import sys; print('a'); print('b'); print('1 fail'); sys.exit(1)")
        verdict, detail = self.R.fast_checks(wt, "x")
        self.assertEqual(verdict, "fail")
        self.assertTrue(detail.startswith("run_checks.py --fast exited 1: a | b | 1 fail"), detail)
        self.assertIn("x.fast.log", detail)

    def test_fast_checks_skips_as_before(self):
        self.assertEqual(self.R.fast_checks(self.tmp / "nowhere")[0], "skipped")
        wt = self.tmp / "nofast"
        (wt / "scripts").mkdir(parents=True)
        (wt / "scripts" / "run_checks.py").write_text("print(1)\n", encoding="utf-8")
        self.assertEqual(self.R.fast_checks(wt)[0], "skipped")

    # ---------------------------------------------------------------- timeout_for

    def test_timeout_for_never_exceeds_the_ceiling(self):
        ceiling = self.R.C["timeout_ceiling_s"]
        for weight in ("light", "heavy"):
            state = {"durations": {weight: [ceiling * 10] * 200}}
            self.assertEqual(self.R.timeout_for(weight, state), ceiling)
            mixed = {"durations": {weight: [60] * 150 + [50_000] * 50}}
            self.assertLessEqual(self.R.timeout_for(weight, mixed), ceiling)
        self.R.C["timeout_s"] = {"light": 10, "heavy": ceiling * 5}  # the starting guess too
        self.assertEqual(self.R.timeout_for("heavy", {"durations": {}}), ceiling)

    def test_timeout_for_never_goes_under_the_floor(self):
        floor = self.R.C["timeout_floor_s"]
        state = {"durations": {"heavy": [1] * 200}}
        self.assertEqual(self.R.timeout_for("heavy", state), floor)

    def test_timeout_for_between_floor_and_ceiling_is_p95_times_factor(self):
        self.R.C.update(timeout_floor_s=10, timeout_ceiling_s=10_000, timeout_p95_factor=3)
        state = {"durations": {"heavy": [100] * 50}}
        self.assertEqual(self.R.timeout_for("heavy", state), 300)


class SchedulerLimitTest(unittest.TestCase):
    """install_task.ps1's ExecutionTimeLimit must outlast the runner's own limits, and the task
    must allow parallel instances (parallel_lanes)."""

    def setUp(self):
        self.script = (HERE / "install_task.ps1").read_text(encoding="utf-8")
        self.cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))

    def limit_s(self) -> int:
        m = re.findall(r"^\$ExecutionTimeLimitMinutes\s*=\s*(\d+)\s*$", self.script, re.M)
        self.assertEqual(len(m), 1, "one named $ExecutionTimeLimitMinutes value at the top of the script")
        return int(m[0]) * 60

    def runner_settings(self) -> str:
        m = re.search(r"^\$settings\s*=\s*New-ScheduledTaskSettingsSet(.*?)(?=^\S)", self.script, re.M | re.S)
        self.assertIsNotNone(m, "the runner's $settings line")
        return m.group(1)

    def test_ceiling_plus_watchdog_fits_the_scheduler_limit(self):
        need = self.cfg["timeout_ceiling_s"] + self.cfg["tick_watchdog_extra_s"]
        self.assertLess(need, self.limit_s(),
                        "timeout_ceiling_s + tick_watchdog_extra_s no longer fits under the scheduled "
                        "task's ExecutionTimeLimit: Windows would end the tick first (NO_REPORT)")

    def test_a_whole_tick_fits_the_scheduler_limit(self):
        """The budget the _timeout_ceiling_s note gives: lane, watchdog extra, gate, reviewer,
        run_checks --fast and the worst-case consult."""
        c = self.cfg
        need = (c["timeout_ceiling_s"] + c["tick_watchdog_extra_s"] + 2 * c["gate_timeout_s"]
                + c["review_timeout_s"] + c["consult_busy_wait_s"] + c["consult_timeout_s"]
                + c["consult_notes_timeout_s"])
        self.assertLessEqual(need, self.limit_s())

    def test_limit_comes_from_the_named_value(self):
        settings = self.runner_settings()
        self.assertIn("$ExecutionTimeLimitMinutes", settings)
        self.assertEqual(len(re.findall(r"-ExecutionTimeLimit\b", settings)), 1)

    def test_policy_is_parallel(self):
        m = re.search(r"-MultipleInstances\s+(\w+)", self.runner_settings())
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "Parallel", "parallel_lanes needs -MultipleInstances Parallel")

    def test_ceiling_is_mirrored_in_the_defaults(self):
        defaults = json.loads((HERE / "config_defaults.json").read_text(encoding="utf-8"))
        self.assertIn("timeout_ceiling_s", defaults)
        self.assertIn("_timeout_ceiling_s", self.cfg)
        self.assertGreaterEqual(self.cfg["timeout_ceiling_s"], self.cfg["timeout_floor_s"])


if __name__ == "__main__":
    unittest.main()
