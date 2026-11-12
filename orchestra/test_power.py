"""Tests for orchestra/power.py and the runner's battery rule (no model calls).

    .venv\\Scripts\\python.exe -m unittest orchestra.test_power -v
The runner tests reuse test_runner's throwaway-repo + fake-lane harness.
"""
import datetime as dt
import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import power  # noqa: E402
import test_runner as TR  # noqa: E402
# Never a real window or toast from a test (orchestra/visible.py: the live window and toasts).
os.environ["ORCHESTRA_LIVE_WINDOW"] = "false"
os.environ["ORCHESTRA_TOASTS"] = "false"


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

    # --- light work on battery above the floor (battery_lane_slots / battery_weights, 12 Nov 2026)

    def add_heavy(self, slug, title):
        p = self.add(slug, title=title)
        p.write_text(p.read_text().replace("Weight: light", "Weight: heavy"))

    def busy_lane(self, task="busy"):
        """Another slot running a task right now (its owner is this live process)."""
        (self.R.REVIEW / "runner.other.lock").write_text(json.dumps(
            {"pid": os.getpid(), "task": task, "lane": "other", "since": dt.datetime.now().isoformat()}))

    def test_battery_switch_on_ac_runs_normally(self):
        self.R.C["dispatch_on_battery"] = True
        self.add_heavy("hac", "heavy on ac")
        os.environ["ORCHESTRA_POWER"] = "ac"
        self.assertEqual(self.R.tick(), "PASS", "on AC weight and battery slots don't matter")

    def test_battery_above_floor_runs_one_light_task(self):
        self.R.C["dispatch_on_battery"] = True
        self.R.C["battery_lane_slots"] = 1
        self.add("l1", title="light one")
        self.add("l2", title="light two")
        os.environ["ORCHESTRA_POWER"] = "battery:80"
        self.assertEqual(self.R.tick(), "PASS")
        self.assertEqual(self.attempts("l1") + self.attempts("l2"), 1, "one light task ran")

    def test_battery_slots_cap_running_lanes(self):
        self.R.C["dispatch_on_battery"] = True
        self.R.C["battery_lane_slots"] = 1
        self.add("lc", title="light capped")
        self.busy_lane()
        os.environ["ORCHESTRA_POWER"] = "battery:80"
        self.assertEqual(self.R.tick(), "on battery", "one lane already runs: the slot is taken")
        self.assertEqual(self.attempts("lc"), 0)
        self.R.C["battery_lane_slots"] = 2
        self.assertEqual(self.R.tick(), "PASS", "a second battery slot lets it start")

    def test_battery_heavy_task_waits(self):
        self.R.C["dispatch_on_battery"] = True
        self.add_heavy("hv", "heavy waits")
        os.environ["ORCHESTRA_POWER"] = "battery:80"
        self.assertIn(self.R.tick(), ("idle", "on battery"))
        self.assertEqual(self.attempts("hv"), 0, "never a heavy run on battery")
        self.assertTrue((self.R.QUEUE / "hv.md").exists())
        os.environ["ORCHESTRA_POWER"] = "ac"
        self.assertEqual(self.R.tick(), "PASS", "it runs once back on AC")

    def test_battery_under_floor_starts_nothing(self):
        self.R.C["dispatch_on_battery"] = True
        self.add("lf", title="light at floor")
        os.environ["ORCHESTRA_POWER"] = f"battery:{self.R.C['battery_floor_percent']}"
        self.assertEqual(self.R.tick(), "on battery")
        self.assertEqual(self.attempts("lf"), 0)

    def test_heavy_block_never_on_battery(self):
        cfg = {"dispatch_on_battery": True, "battery_floor_percent": 30}
        self.assertIsNone(power.heavy_block({"ac": True, "battery_percent": 50}, cfg))
        self.assertTrue(power.heavy_block({"ac": False, "battery_percent": 90}, cfg))
        self.assertTrue(power.heavy_block({"ac": False, "battery_percent": 90}, {}))

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

    # --- one-task override (power.py allow-once, 9 Nov 2026)

    def grant(self, reading="battery:80", **kw):
        os.environ["ORCHESTRA_POWER"] = reading
        return power.allow_once(self.R.REVIEW, self.R.C, power.power_status(), self.R.now(), who="t", **kw)

    def grant_file(self):
        return power.grant_path(self.R.REVIEW, self.R.C)

    def attempts(self, slug):
        return self.R.load_state()["tasks"].get(slug, {}).get("attempts", 0)

    def test_grant_on_battery_starts_one_lane_only_and_is_consumed(self):
        self.add("one", title="one")
        self.add("two", title="two")
        ok, msg = self.grant()
        self.assertTrue(ok, msg)
        self.assertTrue(self.grant_file().exists())
        self.assertIn("pending", power.override_line(self.R.REVIEW, self.R.C, self.R.now()))
        self.assertEqual(self.R.tick(), "PASS")
        self.assertFalse(self.grant_file().exists(), "consumed in the tick that used it")
        self.assertIn("POWER: one-task override used for one (80%)", self.log_text())
        self.assertEqual(self.R.tick(), "on battery", "the next tick waits again")
        self.assertEqual(self.attempts("two"), 0, "only one lane started")
        self.assertTrue((self.R.QUEUE / "two.md").exists())
        self.assertIn("used for one", (self.R.REVIEW / "STATE_NOW.md").read_text(encoding="utf-8"))
        self.assertFalse(self.R.C["dispatch_on_battery"], "the command never turns the switch on")

    def test_grant_still_starts_one_even_heavy(self):
        self.add_heavy("gh", "granted heavy")
        self.add("gl", title="granted light")
        self.assertTrue(self.grant(task="gh")[0])
        self.assertEqual(self.R.tick(), "PASS", "the override is one task, any weight")
        self.assertEqual(self.attempts("gh"), 1)
        self.assertEqual(self.R.tick(), "on battery")
        self.assertEqual(self.attempts("gl"), 0)

    def test_named_task_and_lane(self):
        self.add("aa", title="aa")
        self.add("zz", title="zz")
        self.assertTrue(self.grant(task="zz")[0])
        self.assertEqual(self.R.tick(), "PASS")
        self.assertEqual(self.attempts("zz"), 1)
        self.assertEqual(self.attempts("aa"), 0, "the top task was not the one granted")
        self.assertTrue(self.grant(lane="other")[0])
        self.assertEqual(self.R.tick(), "idle", "no task on that lane: nothing starts")
        self.assertTrue(self.grant_file().exists(), "an unused grant stays until it expires")
        self.assertEqual(self.attempts("aa"), 0)

    def test_expired_grant_is_ignored_and_cleared(self):
        self.add("ex")
        self.assertTrue(self.grant()[0])
        g = json.loads(self.grant_file().read_text(encoding="utf-8"))
        g["expires"] = (dt.datetime.now() - dt.timedelta(minutes=1)).isoformat()
        self.grant_file().write_text(json.dumps(g), encoding="utf-8")
        self.assertIn("expired", power.override_line(self.R.REVIEW, self.R.C, self.R.now()))
        self.assertEqual(self.R.tick(), "on battery")
        self.assertEqual(self.attempts("ex"), 0)
        self.assertFalse(self.grant_file().exists())
        self.assertIn("expired unused", self.log_text())

    def test_expiry_minutes_come_from_config(self):
        self.R.C["power_override_minutes"] = 7
        self.assertTrue(self.grant()[0])
        g = json.loads(self.grant_file().read_text(encoding="utf-8"))
        span = dt.datetime.fromisoformat(g["expires"]) - dt.datetime.fromisoformat(g["created"])
        self.assertEqual(span, dt.timedelta(minutes=7))
        self.assertEqual((g["who"], g["task"], g["lane"]), ("t", None, None))

    def test_below_the_floor_refused(self):
        ok, msg = self.grant("battery:20")
        self.assertFalse(ok)
        self.assertIn("floor", msg)
        self.assertFalse(self.grant_file().exists())
        # granted above the floor, then the battery drops: the grant is not used
        self.add("fl")
        self.assertTrue(self.grant("battery:60")[0])
        os.environ["ORCHESTRA_POWER"] = "battery:25"
        self.assertEqual(self.R.tick(), "on battery")
        self.assertEqual(self.attempts("fl"), 0)
        self.assertTrue(self.grant_file().exists())

    def test_on_ac_nothing_written_and_grant_not_used_up(self):
        ok, msg = self.grant("ac")
        self.assertFalse(ok)
        self.assertIn("Nothing to override", msg)
        self.assertFalse(self.grant_file().exists())
        self.add("ac1")
        self.assertTrue(self.grant()[0])
        os.environ["ORCHESTRA_POWER"] = "ac"
        self.assertEqual(self.R.tick(), "PASS")
        self.assertTrue(self.grant_file().exists(), "on AC the grant is not used up")
        self.assertNotIn("one-task override used", self.log_text())

    def test_grant_used_by_another_tick_first(self):
        self.add("ra")
        self.assertTrue(self.grant()[0])
        g = self.R.power_override()
        self.assertIsNotNone(g)
        power.drop_grant(self.R.REVIEW, self.R.C)  # another tick consumed it meanwhile
        with unittest.mock.patch.object(self.R, "power_override", return_value=g):
            self.assertEqual(self.R.tick(), "on battery", "re-checked under the dispatch lock")
        self.assertEqual(self.attempts("ra"), 0)

    def test_status_cli_and_watch_line(self):
        import io, contextlib, watch
        self.assertTrue(self.grant(task="x1")[0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(power.main(["status"]), 0)
        self.assertIn("pending for x1", out.getvalue())
        with unittest.mock.patch.object(watch, "REPO", self.repo), \
                unittest.mock.patch.object(watch, "REVIEW", self.R.REVIEW):
            self.assertIn("pending for x1", watch.override_line())
        os.environ["ORCHESTRA_POWER"] = "ac"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(power.main(["allow-once"]), 1)
        self.assertIn("Nothing to override", out.getvalue())


if __name__ == "__main__":
    unittest.main()
