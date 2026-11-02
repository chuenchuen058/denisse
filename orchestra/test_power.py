"""Tests for orchestra/power.py and the runner's battery rule (no model calls).

    .venv\\Scripts\\python.exe -m unittest orchestra.test_power -v
The runner tests reuse test_runner's throwaway-repo + fake-lane harness.
"""
import datetime as dt
import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import power  # noqa: E402
import test_runner as TR  # noqa: E402


def sps(ac, flag, pct):
    s = power.SYSTEM_POWER_STATUS()
    s.ACLineStatus, s.BatteryFlag, s.BatteryLifePercent = ac, flag, pct
    return s


class PowerStatusTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("ORCHESTRA_POWER", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["ORCHESTRA_POWER"] = self._env

    def test_parse_on_battery(self):
        self.assertEqual(power.parse(sps(0, 0, 87)), {"ac": False, "battery_percent": 87})

    def test_parse_plugged_in_charging(self):
        self.assertEqual(power.parse(sps(1, 8, 55)), {"ac": True, "battery_percent": 55})

    def test_parse_desktop_without_battery(self):
        self.assertEqual(power.parse(sps(1, 128, 255)), {"ac": True, "battery_percent": None})

    def test_parse_unknown(self):
        st = power.parse(sps(255, 255, 255))
        self.assertEqual(st, {"ac": None, "battery_percent": None})
        self.assertFalse(power.on_battery(st), "unknown counts as AC")
        self.assertIsNone(power.dispatch_block(st, {}))

    def test_real_reading_has_the_shape(self):
        st = power.power_status()
        self.assertEqual(set(st), {"ac", "battery_percent"})
        self.assertIn(st["ac"], (True, False, None))
        self.assertTrue(st["battery_percent"] is None or 0 <= st["battery_percent"] <= 100)

    def test_env_override(self):
        for text, want in (("ac", {"ac": True, "battery_percent": None}),
                           ("battery:42", {"ac": False, "battery_percent": 42}),
                           ("battery", {"ac": False, "battery_percent": None}),
                           ("unknown", {"ac": None, "battery_percent": None})):
            os.environ["ORCHESTRA_POWER"] = text
            self.assertEqual(power.power_status(), want, text)
        os.environ.pop("ORCHESTRA_POWER")

    def test_dispatch_block_and_floor(self):
        bat = {"ac": False, "battery_percent": 87}
        self.assertTrue(power.dispatch_block(bat, {}), "default: dispatch_on_battery off")
        self.assertIsNone(power.dispatch_block(bat, {"dispatch_on_battery": True}))
        low = {"ac": False, "battery_percent": 30}
        self.assertIn("floor", power.dispatch_block(low, {"dispatch_on_battery": True}))
        self.assertIsNone(power.dispatch_block({"ac": True, "battery_percent": 10}, {}))

    def test_describe(self):
        self.assertEqual(power.describe({"ac": True, "battery_percent": 96}, {}), "AC")
        self.assertEqual(power.describe({"ac": False, "battery_percent": 87}, {}),
                         "battery 87% (dispatch paused)")
        self.assertEqual(power.describe({"ac": False, "battery_percent": 87}, {"dispatch_on_battery": True}),
                         "battery 87% (dispatching)")


class RunnerOnBatteryTest(unittest.TestCase):
    # test_runner's harness, without inheriting (and so re-running) its tests
    setUp, tearDown, add, state = TR.RunnerTest.setUp, TR.RunnerTest.tearDown, TR.RunnerTest.add, TR.RunnerTest.state
    dead_pid, write_lock, orphan_worktree = TR.RunnerTest.dead_pid, TR.RunnerTest.write_lock, TR.RunnerTest.orphan_worktree

    def log_text(self):
        return self.R.LOG_FILE.read_text(encoding="utf-8") if self.R.LOG_FILE.exists() else ""

    def test_no_dispatch_on_battery(self):
        self.add("bt")
        os.environ["ORCHESTRA_POWER"] = "battery:87"
        self.assertEqual(self.R.tick(), "on battery")
        self.assertTrue((self.R.QUEUE / "bt.md").exists())
        self.assertEqual(self.R.load_state()["tasks"].get("bt", {}).get("attempts", 0), 0, "not started")
        self.assertFalse(self.R.LOCK_FILE.exists())
        self.assertIn("ON BATTERY: no new dispatch (87%)", self.log_text())
        self.assertTrue((self.R.REVIEW / "STATE_NOW.md").exists(), "STATE NOW still refreshed")

    def test_dispatch_on_ac_and_unknown(self):
        for i, reading in enumerate(("ac", "unknown")):
            self.add(f"ok{i}", title=f"ok {reading}")  # distinct content: same text counts as done
            os.environ["ORCHESTRA_POWER"] = reading
            self.assertEqual(self.R.tick(), "PASS", reading)
        self.assertNotIn("ON BATTERY", self.log_text())

    def test_dispatch_on_battery_when_allowed_until_floor(self):
        self.add("al")
        self.R.C["dispatch_on_battery"] = True
        os.environ["ORCHESTRA_POWER"] = "battery:25"  # under the 30% floor
        self.assertEqual(self.R.tick(), "on battery")
        os.environ["ORCHESTRA_POWER"] = "battery:80"
        self.assertEqual(self.R.tick(), "PASS")

    def test_on_battery_log_is_throttled(self):
        self.add("th")
        os.environ["ORCHESTRA_POWER"] = "battery:70"
        for _ in range(3):
            self.assertEqual(self.R.tick(), "on battery")
        self.assertEqual(self.log_text().count("ON BATTERY"), 1, "once per 30 min, not every tick")
        # 31 minutes later it logs again
        old = (dt.datetime.now() - dt.timedelta(minutes=31)).isoformat()
        self.R.POWER_FILE.write_text(json.dumps({"logged": old}))
        self.R.tick()
        self.assertEqual(self.log_text().count("ON BATTERY"), 2)
        # back on AC: the throttle resets and the task runs
        os.environ["ORCHESTRA_POWER"] = "ac"
        self.assertEqual(self.R.tick(), "PASS")
        self.assertFalse(self.R.POWER_FILE.exists())
        self.assertIn("dispatching again", self.log_text())

    def test_finished_lane_still_integrates_on_battery(self):
        self.add("fin")
        self.add("next")  # must not be started
        self.orphan_worktree("fin", report="RESULT: PASS\nGATE: ok\nHUMAN: none\nPREMISE: right\n", commit=True)
        self.write_lock(pid=self.dead_pid(), created=1, task="fin", lane="fake",
                        lane_pid=self.dead_pid(), lane_created=1, before=[])
        os.environ["ORCHESTRA_POWER"] = "battery:50"
        self.assertEqual(self.R.tick(), "on battery")
        self.assertTrue((self.repo / "done.txt").exists(), "the finished lane's branch is merged")
        st = self.state()["tasks"]
        self.assertEqual(st["fin"]["merge"], "merged")
        self.assertEqual(st.get("next", {}).get("attempts", 0), 0, "no new dispatch")
        self.assertTrue((self.R.QUEUE / "next.md").exists())


if __name__ == "__main__":
    unittest.main()
