"""Tests for orchestra/visible.py: the live window and toasts. Fakes only: no window is
opened and no toast is shown (visible.SPAWN / visible.RUN are replaced).

    .venv\\Scripts\\python.exe -m unittest orchestra.test_visible -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import visible  # noqa: E402
import test_runner as TR  # noqa: E402  (sets ORCHESTRA_LIVE_WINDOW / ORCHESTRA_TOASTS false for the runner)


@dataclass
class Task:
    slug: str = "2026-11-09T1000-x"
    title: str = "Fix the tray restart"
    lane: str = "claude"


def shipped() -> dict:
    return {k: v for k, v in json.loads((HERE / "config.json").read_text(encoding="utf-8")).items()
            if not k.startswith("_")}


class Done:
    def __init__(self, code=0, err=""):
        self.returncode, self.stderr, self.stdout = code, err, ""


class VisibleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="visible-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.review = self.tmp / ".review"
        self.cfg = {**shipped(), "live_window": True, "toasts": True}
        self.log, self.spawned, self.ran = [], [], []
        self.procs = {}  # pid -> creation time of the fake processes still "running"
        self.next_pid = 4000
        guard = mock.patch.multiple(visible, SPAWN=self.no_real, RUN=self.no_real)
        guard.start()
        self.addCleanup(guard.stop)

    def no_real(self, *a, **k):
        raise AssertionError("a test must never open a real window or toast")

    def spawn(self, argv, env):
        self.next_pid += 1
        self.procs[self.next_pid] = self.next_pid * 10
        self.spawned.append((argv, env))
        return self.next_pid

    def alive(self, pid):
        return self.procs.get(pid)

    def fake_run(self, argv, **kw):
        self.ran.append((argv, kw))
        return Done()

    def window(self, **cfg):
        return visible.ensure_window({**self.cfg, **cfg}, self.review, self.tmp, self.log.append, self.spawn, self.alive)

    # --- shipped config

    def test_shipped_switches_are_on_and_in_both_files(self):
        defaults = json.loads((HERE / "config_defaults.json").read_text(encoding="utf-8"))
        for key in ("live_window", "live_window_title", "live_window_argv", "toasts", "toast_started",
                    "toast_finished", "toast_argv", "toast_timeout_s"):
            self.assertIn(key, defaults)
            self.assertEqual(shipped()[key], defaults[key], key)
        self.assertIs(defaults["live_window"], True)
        self.assertIs(defaults["toasts"], True)
        self.assertEqual(defaults["live_window_title"], "Orchestra live")
        self.assertTrue((HERE / "toast.ps1").exists())
        self.assertEqual(defaults["toast_started"].format(lane="codex", title="T"), "Orchestra: codex started T")
        self.assertIn("finished: PASS", defaults["toast_finished"].format(lane="codex", title="T", result="PASS"))

    # --- one window, reused

    def test_opens_once_then_reuses_the_open_window(self):
        self.assertEqual(self.window(), "opened")
        argv, env = self.spawned[0]
        self.assertIn(str(HERE / "watch.py"), argv)
        self.assertEqual(argv[argv.index("--title") + 1], "Orchestra live")
        self.assertEqual(env["ORCHESTRA_REPO"], str(self.tmp))
        for _ in range(3):
            self.assertEqual(self.window(), "open")
        self.assertEqual(len(self.spawned), 1, "never a second window while the first is open")
        self.assertEqual(len(self.log), 1)

    def test_a_closed_window_or_reused_pid_is_opened_again(self):
        self.window()
        self.procs.clear()  # Chuen closed it
        self.assertEqual(self.window(), "opened")
        pid = json.loads((self.review / "live_window.json").read_text())["pid"]
        self.procs[pid] = 1  # same pid, another process
        self.assertEqual(self.window(), "opened")
        self.assertEqual(len(self.spawned), 3)

    def test_off_or_failing_never_raises(self):
        self.assertEqual(self.window(live_window=False), "off")
        self.assertEqual(self.spawned, [])
        boom = mock.Mock(side_effect=OSError("no desktop"))
        self.assertEqual(visible.ensure_window(self.cfg, self.review, self.tmp, self.log.append, boom, self.alive),
                         "failed")
        self.assertIn("LIVE window not opened (OSError: no desktop)", self.log[-1])

    def test_window_is_shown_without_taking_focus(self):
        if os.name != "nt":
            self.skipTest("Windows only")
        with mock.patch.object(visible.subprocess, "Popen") as popen:
            popen.return_value.pid = 7
            self.assertEqual(visible.spawn_window(["x"], {}), 7)
        kw = popen.call_args.kwargs
        self.assertTrue(kw["creationflags"] & visible.CREATE_NEW_CONSOLE)
        self.assertEqual(kw["startupinfo"].wShowWindow, visible.SW_SHOWNOACTIVATE)

    # --- toasts

    def test_start_and_finish_toasts(self):
        visible.lane_started(Task(), self.cfg, self.review, self.tmp, self.log.append, self.spawn, self.alive, self.fake_run)
        visible.lane_finished(Task(), "PARTIAL", self.cfg, self.log.append, self.fake_run)
        texts = [a[a.index("-Text") + 1] for a, _ in self.ran]
        self.assertEqual(texts, ["Orchestra: claude started Fix the tray restart",
                                 "Orchestra: claude finished: PARTIAL Fix the tray restart"])
        self.assertIn(str(HERE / "toast.ps1"), self.ran[0][0])
        self.assertEqual(self.ran[0][1]["timeout"], self.cfg["toast_timeout_s"])

    def test_a_lane_with_its_own_tab_does_not_also_open_the_live_window(self):
        """One window per run (config live_window_with_lane_tab false): the tab shows the lane, so
        the live window opens only for a lane that gets no tab. The toasts are not affected."""
        cfg = {**self.cfg, "lane_tabs": True, "lane_tab_lanes": ["codex"], "live_window_with_lane_tab": False}
        started = lambda c, lane: visible.lane_started(Task(lane=lane), c, self.review, self.tmp, self.log.append,  # noqa: E731
                                                       self.spawn, self.alive, self.fake_run)
        started(cfg, "codex")
        self.assertEqual(self.spawned, [], "codex gets a tab: no live window")
        self.assertEqual(len(self.ran), 1, "the start toast still goes out")
        started(cfg, "gemini")
        self.assertEqual(len(self.spawned), 1, "no tab for this lane: the live window opens")
        self.procs.clear()
        self.spawned.clear()
        started({**cfg, "lane_tabs": False}, "codex")
        self.assertEqual(len(self.spawned), 1, "tabs off: the live window is the only view")
        self.procs.clear()
        self.spawned.clear()
        started({**cfg, "live_window_with_lane_tab": True}, "codex")
        self.assertEqual(len(self.spawned), 1, "true = both, as before")
        defaults = json.loads((HERE / "config_defaults.json").read_text(encoding="utf-8"))
        self.assertIs(defaults["live_window_with_lane_tab"], False)

    def test_toast_off_failing_or_hanging_is_a_log_line_at_most(self):
        self.assertFalse(visible.toast({**self.cfg, "toasts": False}, "x", self.log.append, self.fake_run))
        self.assertEqual(self.ran, [])
        self.assertFalse(visible.toast(self.cfg, "x", self.log.append, lambda *a, **k: Done(1, "no WinRT")))
        self.assertIn("TOAST failed (exit 1): no WinRT", self.log[-1])
        hang = mock.Mock(side_effect=subprocess.TimeoutExpired("powershell", 30))
        self.assertFalse(visible.toast(self.cfg, "x", self.log.append, hang))
        self.assertIn("TimeoutExpired", self.log[-1])


class RunnerVisibleTest(TR.RunnerTest.__base__):
    """The runner opens the window and toasts at a real tick (fake lane, fake window/toast)."""

    setUp = TR.RunnerTest.setUp
    tearDown = TR.RunnerTest.tearDown
    add = TR.RunnerTest.add
    state = TR.RunnerTest.state

    def test_tick_opens_the_window_once_and_toasts_start_and_finish(self):
        spawned, ran = [], []
        alive = {}

        def spawn(argv, env):
            alive[9001] = 1
            spawned.append(argv)
            return 9001

        guard = mock.patch.multiple(self.R.visible, SPAWN=spawn,
                                    RUN=lambda argv, **kw: ran.append(argv) or Done())
        guard.start()
        self.addCleanup(guard.stop)
        live = mock.patch.object(self.R, "proc_created", lambda pid: alive.get(pid))
        live.start()
        self.addCleanup(live.stop)
        self.R.C.update(live_window=True, toasts=True)
        self.add("a")
        self.assertEqual(self.R.tick(), "PASS")
        self.add("b", extra=" Another one.")
        self.assertEqual(self.R.tick(), "PASS")
        self.assertEqual(len(spawned), 1, "one window, reused")
        texts = [a[a.index("-Text") + 1] for a in ran]
        self.assertEqual(len(texts), 4)
        self.assertTrue(texts[0].startswith("Orchestra: fake started "))
        self.assertTrue(texts[1].startswith("Orchestra: fake finished: PASS "))

    def test_off_in_config_means_nothing_is_opened(self):
        guard = mock.patch.multiple(self.R.visible, SPAWN=mock.Mock(side_effect=AssertionError("window")),
                                    RUN=mock.Mock(side_effect=AssertionError("toast")))
        guard.start()
        self.addCleanup(guard.stop)
        self.assertIs(self.R.C["live_window"], False, "the env switch in the test modules reads 'false' as off")
        self.assertIs(self.R.C["toasts"], False)
        self.add("c")
        self.assertEqual(self.R.tick(), "PASS")


if __name__ == "__main__":
    unittest.main()
