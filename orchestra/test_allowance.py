"""Tests for orchestra/allowance.py and the runner's use of it: fake clocks, fake lane logs,
a throwaway allowance file. No model calls.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_allowance
"""
# checks: slow  (the runner-level cases build real git repos via test_runner's fixture)
import datetime as dt
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import allowance as A  # noqa: E402
import test_runner as TR  # noqa: E402

CFG = {k: v for k, v in json.loads((HERE / "config_defaults.json").read_text(encoding="utf-8")).items()
       if not k.startswith("_")}
# Monday 9 Nov 2026 12:00, local; Codex's weekly window resets Wed 11 Nov 12:00 (48 h later).
NOW = dt.datetime(2026, 11, 9, 12, 0)
RESET = dt.datetime(2026, 11, 11, 12, 0)


class AllowanceModel(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "allowance.json"
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def data(self):
        return A.load(self.path)

    def test_ten_percent_two_days_out_holds_codex_until_the_last_hours_then_spends_it(self):
        A.record_set(self.path, CFG, "codex", NOW, 10, RESET)  # "codex: 10% weekly left, resets Wed 12:00"
        held = A.hold(CFG, self.data(), "codex", NOW, weight="light")
        self.assertEqual(held, "codex held: 10% weekly left, reset in 48h")
        self.assertEqual(A.estimate(CFG, self.data(), "codex", NOW)["mode"], "conserve")
        # still held the next day ...
        self.assertIn("codex held", A.hold(CFG, self.data(), "codex", NOW + dt.timedelta(hours=30)))
        # ... and spent in the last hours before the reset (allowance_spend_down_hours)
        late = RESET - dt.timedelta(hours=float(CFG["allowance_spend_down_hours"]) - 1)
        self.assertIsNone(A.hold(CFG, self.data(), "codex", late, weight="light"))
        est = A.estimate(CFG, self.data(), "codex", late)
        self.assertEqual(est["mode"], "spend")
        self.assertIn("use it or lose it", est["why"])
        # the planner then prefers it over a lane with plenty left and fewer runs
        lane, why = A.choose(CFG, self.data(), ["claude", "codex"], late, recent={"claude": 0, "codex": 9})
        self.assertEqual((lane, why), ("codex", est["why"]))
        # after the reset the window is full again and the plan is back to spreading
        after = RESET + dt.timedelta(hours=1)
        est = A.estimate(CFG, self.data(), "codex", after)
        self.assertEqual((est["mode"], est["left"]), ("spread", 100.0))
        self.assertEqual(est["windows"]["weekly"]["resets"], RESET + dt.timedelta(days=7))

    def test_runs_are_subtracted_and_a_weight_that_wont_fit_is_held(self):
        A.record_set(self.path, CFG, "codex", NOW, 30, RESET)
        heavy = CFG["allowance"]["codex"]["windows"]["weekly"]["run_pct"]["heavy"]
        for i in range(2):
            A.record_run(self.path, CFG, "codex", NOW + dt.timedelta(minutes=10 + i), weight="heavy")
        left = A.estimate(CFG, self.data(), "codex", NOW + dt.timedelta(hours=1))["left"]
        self.assertAlmostEqual(left, 30 - 2 * heavy)
        # the 5-hour window: a heavy run needs more than a light one has left
        A.record_limit(self.path, CFG, "codex", NOW, NOW + dt.timedelta(hours=2), "try again at 2:00 PM")
        self.assertIn("held: a heavy run needs", A.hold(CFG, self.data(), "codex", NOW + dt.timedelta(hours=1),
                                                        weight="heavy", priority="urgent"))

    def test_urgent_tasks_may_use_the_reserve_and_no_further(self):
        A.record_set(self.path, CFG, "codex", NOW, 10, RESET)
        self.assertIsNone(A.hold(CFG, self.data(), "codex", NOW, priority="urgent", weight="light"))
        A.record_set(self.path, CFG, "codex", NOW + dt.timedelta(minutes=1), 6, RESET)
        self.assertIn("reserve", A.hold(CFG, self.data(), "codex", NOW + dt.timedelta(minutes=2),
                                        priority="urgent", weight="heavy"))

    def test_a_limit_errors_try_again_time_is_learned(self):
        # Codex: "try again at 9:24 PM" said at 18:00 -> the 5-hour window is empty until 21:24
        at = dt.datetime(2026, 11, 9, 18, 0)
        msg = "ERROR: You've hit your usage limit. ... or try again at 9:24 PM."
        sys.path.insert(0, str(HERE))
        import runner
        until = runner.parse_limit_until(msg, at)
        A.record_limit(self.path, CFG, "codex", at, until, msg)
        w = A.estimate(CFG, self.data(), "codex", at + dt.timedelta(minutes=5))["windows"]["five_hour"]
        self.assertEqual((w["left"], w["resets"]), (0.0, dt.datetime(2026, 11, 9, 21, 24)))
        # a wait of days belongs to the weekly window
        A.record_limit(self.path, CFG, "codex", at, RESET, "try again at Nov 11th, 2026 12:00 PM")
        self.assertEqual(self.data()["readings"]["codex"]["weekly"]["resets"], RESET.isoformat())
        # Claude names its window
        A.record_limit(self.path, CFG, "claude", at, at + dt.timedelta(days=3), "Claude usage limit reached (seven_day)|1")
        self.assertIn("seven_day", self.data()["readings"]["claude"])

    def test_claude_reports_its_own_windows_in_the_lane_log(self):
        five, week = int((NOW + dt.timedelta(hours=2)).timestamp()), int(RESET.timestamp())
        log = "\n".join([
            json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": 0.30, "resetsAt": five}, "seven_day": {"utilization": 0.80, "resetsAt": week}}}}),
            json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": 0.46, "resetsAt": five}, "seven_day": {"utilization": 0.81, "resetsAt": week}}}}),
            json.dumps({"type": "result", "total_cost_usd": 1.0, "num_turns": 7})])
        A.record_run(self.path, CFG, "claude", NOW, weight="heavy", model="claude-opus-5-5", turns=7, log_text=log)
        est = A.estimate(CFG, self.data(), "claude", NOW)
        self.assertAlmostEqual(est["windows"]["five_hour"]["left"], 54.0)
        self.assertAlmostEqual(est["windows"]["seven_day"]["left"], 19.0)
        self.assertEqual(est["mode"], "conserve")  # 19% weekly, 48 h out
        self.assertEqual(self.data()["events"][-1]["turns"], 7)

    def test_model_factor_counts_a_smaller_model_for_less(self):
        f = CFG["allowance"]["claude"]["model_factor"]
        self.assertLess(A.run_cost(CFG, "claude", "five_hour", "light", "claude-sonnet-5-5"),
                        A.run_cost(CFG, "claude", "five_hour", "light", "claude-opus-5-5"))
        self.assertEqual(A.model_factor(CFG, "claude", "claude-haiku-5-5"), f["haiku"])
        self.assertEqual(A.model_factor(CFG, "claude", None), 1.0)

    def test_unknown_lanes_and_windows_never_hold(self):
        self.assertIsNone(A.hold(CFG, self.data(), "codex", NOW, weight="heavy"))
        self.assertIsNone(A.hold(CFG, self.data(), "no-such-lane", NOW))
        self.assertEqual(A.estimate(CFG, self.data(), "codex", NOW)["mode"], "spread")

    def test_spend_down_ties_go_to_the_soonest_reset_then_the_most_left(self):
        late = NOW + dt.timedelta(hours=1)
        A.record_set(self.path, CFG, "codex", NOW, 40, NOW + dt.timedelta(hours=4))
        A.record_set(self.path, CFG, "copilot", NOW, 40, NOW + dt.timedelta(hours=3))
        self.assertEqual(A.choose(CFG, self.data(), ["codex", "copilot"], late)[0], "copilot")
        A.record_set(self.path, CFG, "copilot", NOW, 60, NOW + dt.timedelta(hours=4))
        self.assertEqual(A.choose(CFG, self.data(), ["codex", "copilot"], late)[0], "copilot")

    def test_parse_when(self):
        self.assertEqual(A.parse_when("Wed 12:00", NOW), RESET)
        self.assertEqual(A.parse_when("2026-11-11T12:00", NOW), RESET)
        self.assertEqual(A.parse_when("Mon 11:00", NOW), NOW + dt.timedelta(days=7, hours=-1))
        self.assertIsNone(A.parse_when("someday", NOW))

    def test_the_hub_snapshot_lists_the_plan_per_lane(self):
        from unittest import mock
        sys.path.insert(0, str(HERE.parent))
        from atrium import snapshot
        at = dt.datetime.now().replace(microsecond=0)
        A.record_set(self.path, CFG, "codex", at, 10, at + dt.timedelta(hours=41))
        # _usage also probes the local Ollama for its own row: offline here, as in atrium's tests
        # (run_checks refuses a real network connect and marks the module GUARD).
        with mock.patch.object(snapshot, "REVIEW", self.tmp), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("no Ollama in tests")):
            rows = {r["lane"]: r for r in snapshot._usage([])}
        self.assertEqual((rows["codex"]["allowance_plan"], rows["codex"]["allowance_left_pct"]), ("conserve", 10.0))
        self.assertIn("codex held: 10% weekly left", rows["codex"]["allowance_why"])
        self.assertIsNotNone(rows["codex"]["allowance_resets"])

    def test_learn_reads_old_lane_logs(self):
        log = self.tmp / "x.lane.log"
        log.write_text("ERROR: You've hit your usage limit. try again at 11:59 PM.\n", encoding="utf-8")
        sys.path.insert(0, str(HERE))
        import runner
        n = A.learn_logs(self.path, CFG, [log], runner.parse_limit_until, lambda p, t: "codex")
        self.assertEqual(n, 1)
        self.assertTrue(any(r["source"] == "limit error" for r in self.data()["readings"]["codex"].values()))


class AllowanceLock(unittest.TestCase):
    """The allowance file's lock (reviews, 12 and 16 Nov 2026): a writer that can't get it skips
    its write instead of writing unlocked, only the holder ever deletes it, and an old lock is
    taken over only when the writer it names is gone."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="allowance-lock-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "allowance.json"
        self.lock = self.path.with_suffix(".lock")
        self.cfg = {**CFG, "allowance_lock_wait_s": 0.2}

    def age(self, seconds):
        old = dt.datetime.now().timestamp() - seconds
        os.utime(self.lock, (old, old))

    def test_a_busy_lock_skips_the_write_and_is_left_alone(self):
        A.record_set(self.path, self.cfg, "codex", dt.datetime(2026, 11, 12, 9), 50, None)
        before = self.path.read_text(encoding="utf-8")
        other = A._Lock(self.path, wait=0.2)
        other.__enter__()  # another writer holds it
        try:
            theirs = self.lock.read_text(encoding="utf-8")
            with self.assertRaises(A.LockBusy):
                A.record_set(self.path, self.cfg, "codex", dt.datetime(2026, 11, 12, 10), 1, None)
            self.assertEqual(self.path.read_text(encoding="utf-8"), before, "nothing written unlocked")
            self.assertEqual(self.lock.read_text(encoding="utf-8"), theirs, "the other writer's lock was not deleted")
        finally:
            other.__exit__(None, None, None)
        self.assertFalse(self.lock.exists(), "its holder released it")

    def test_exit_never_deletes_a_lock_another_writer_took_over(self):
        mine = A._Lock(self.path)
        mine.__enter__()
        self.lock.write_text("someone-else", encoding="utf-8")  # e.g. taken over as stale meanwhile
        mine.__exit__(None, None, None)
        self.assertEqual(self.lock.read_text(encoding="utf-8"), "someone-else")

    def test_an_old_lock_naming_no_owner_is_taken_over(self):
        self.lock.write_text("dead-writer", encoding="utf-8")  # written before owners were recorded
        self.age(3600)
        A.record_set(self.path, self.cfg, "codex", dt.datetime(2026, 11, 12, 9), 50, None)
        self.assertEqual(A.load(self.path)["events"][-1]["left"], 50)
        self.assertFalse(self.lock.exists())

    def test_an_old_lock_whose_writer_is_still_running_is_not_taken(self):
        """Review, 16 Nov 2026: a slow learn_logs() holds the lock past allowance_lock_stale_s while
        alive; its lock must not be reclaimed under it."""
        holder = A._Lock(self.path)  # this very process: alive, by the runner's own rule
        holder.__enter__()
        try:
            self.age(3600)
            with self.assertRaises(A.LockBusy):
                A.record_set(self.path, self.cfg, "codex", dt.datetime(2026, 11, 12, 9), 50, None)
            self.assertEqual(self.lock.read_text(encoding="utf-8"), holder.token)
            self.assertFalse(self.path.exists(), "nothing written")
        finally:
            holder.__exit__(None, None, None)
        self.assertFalse(self.lock.exists())

    def test_an_old_lock_whose_writer_is_gone_is_taken_over(self):
        # The pid exists (this process) but not with that creation time: a reused pid, not the writer.
        self.lock.write_text(json.dumps({"pid": os.getpid(), "created": 1, "token": "x"}), encoding="utf-8")
        self.age(3600)
        A.record_set(self.path, self.cfg, "codex", dt.datetime(2026, 11, 12, 9), 50, None)
        self.assertEqual(A.load(self.path)["events"][-1]["left"], 50)
        self.assertFalse(self.lock.exists())

    def test_a_young_lock_is_never_taken_even_if_its_writer_looks_gone(self):
        self.lock.write_text(json.dumps({"pid": os.getpid(), "created": 1, "token": "x"}), encoding="utf-8")
        with self.assertRaises(A.LockBusy):
            A.record_set(self.path, self.cfg, "codex", dt.datetime(2026, 11, 12, 9), 50, None)
        self.assertTrue(self.lock.exists())


class AllowanceInTheRunner(unittest.TestCase):
    """route / why_not_runnable / tick with the allowance plan, in test_runner's throwaway repo."""
    add, state, why = TR.RunnerTest.add, TR.RunnerTest.state, TR.RunnerTest.why

    def setUp(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        TR.RunnerTest.setUp(self)
        self.lanes_with("codex", "claude")

    def tearDown(self):
        TR.RunnerTest.tearDown(self)

    def lanes_with(self, *names):
        lanes = {n: [sys.executable, "-c", TR.FAKE] for n in ("fake", *names)}
        (self.R.REVIEW / "lanes.json").write_text(json.dumps(lanes))

    def say(self, lane, left, hours):
        A.record_set(self.R.allowance_path(), self.R.C, lane, self.R.now(), left,
                     self.R.now() + dt.timedelta(hours=hours))

    def test_an_explicit_held_lane_falls_back_and_says_why(self):
        self.say("codex", 10, 41)
        self.add("fb", lane="codex")
        self.add("fixed", lane="codex", headers="Lane-Fixed: yes\n", extra=" fixed")
        self.assertIsNone(self.why("fb"), "claude takes it")
        self.assertIn("codex held: 10% weekly left, reset in 41h", self.why("fixed"))
        os.environ["FAKE_HOOKLOG"] = "1"
        self.assertEqual(self.R.tick(), "PASS")
        log = self.R.LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("START fb lane=claude (fallback: codex held: 10% weekly left, reset in 41h)", log)
        self.assertEqual(self.state()["tasks"]["fb"]["history"][-1]["fallback_from"], "codex")
        self.assertEqual(self.R.tick(), "idle", "the Lane-Fixed task waits for codex")
        ev = A.load(self.R.allowance_path())["events"]
        self.assertEqual([e["type"] for e in ev if e["type"] == "run"], ["run"], "the run's usage was recorded")

    def test_lane_auto_spends_the_lane_about_to_reset(self):
        self.say("codex", 40, 2)
        self.add("planned", lane="auto")
        self.assertIsNone(self.R.header_problem(self.R.load_queue()[0]))
        t = self.R.route(self.R.load_queue(), self.R.load_state())[0]
        self.assertEqual(t.lane, "codex")
        # Merged onto master's scored lane pick: Lane: auto is lane_pick's, and the plan's
        # spend-down is a score part there (allowance_spend_bonus), so the why is pick-why.
        self.assertIn("use it or lose it", t.fields["pick-why"])
        self.assertIn("allowance +", t.fields["pick-why"])

    def test_lane_auto_with_every_lane_held_waits_with_the_reasons(self):
        # lane_pick scores Lane: auto over every configured lane (the branch's allowance_plan_lanes
        # pool went with its own planner), so the only lane here is the held one.
        only = {"codex": [sys.executable, "-c", TR.FAKE]}
        self.say("codex", 3, 41)
        self.add("planned", lane="auto")
        with unittest.mock.patch.object(self.R, "lanes", lambda: dict(only)):
            why = self.why("planned")
        self.assertIn("no lane available", why)
        self.assertIn("codex allowance: codex held", why)

    def test_a_bugfix_on_claude_is_never_held_by_the_plan(self):
        self.say("claude", 5, 100)
        self.add("bug", lane="claude", kind="bugfix")
        self.add("work", lane="claude", extra=" work")
        self.R.C["lane_fallback"] = {}
        self.assertIsNone(self.why("bug"))
        self.assertIn("claude held", self.why("work"))

    def test_model_by_weight_and_kind_unless_the_header_says(self):
        self.add("light", lane="claude")
        self.add("bug", lane="claude", kind="bugfix", extra=" bug")
        self.add("pinned", lane="claude", headers="Model: fable\n", extra=" pinned")
        got = {t.slug: self.R.choose_model(t) for t in self.R.load_queue()}
        self.assertEqual(got["light"], (self.R.C["claude_model_by_weight"]["light"], "light task (claude_model_by_weight)"))
        self.assertEqual(got["bug"], (self.R.C["claude_model_by_kind"]["bugfix"], "bugfix task (claude_model_by_kind)"))
        self.assertEqual(got["pinned"], ("fable", "Model: header"))
        self.assertEqual(self.R.choose_model(self.R.load_queue()[0], "codex"), (None, ""))

    def test_lane_slots_and_campaign_overrides_still_apply(self):
        stop = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        import test_campaign as TC
        camp = TC.sprint(overrides={"lane_slots": {"codex": 3}}, started_at=start, stop_at=stop)
        (self.R.REVIEW / "campaign.json").write_text(json.dumps(camp), encoding="utf-8")
        self.say("codex", 10, 41)
        cfg = self.R.load_config(log_fn=lambda m: None)
        self.assertEqual(cfg["lane_slots"]["codex"], 3)
        self.assertEqual(cfg["allowance"], self.R.C["allowance"], "the allowance model is untouched by a campaign")

    def test_status_and_state_now_show_the_plan(self):
        self.say("codex", 10, 41)
        import status
        importlib.reload(status)
        rows = {r["lane"]: r for r in status.collect()["allowance"]}
        self.assertEqual((rows["codex"]["mode"], rows["codex"]["left_pct"]), ("conserve", 10.0))
        self.assertIn("conserved -- codex held: 10% weekly left", status.render(status.collect()))
        self.R.write_state_now()
        self.assertIn("**Allowance:**", (self.R.REVIEW / "STATE_NOW.md").read_text(encoding="utf-8"))

    def test_a_routed_held_task_does_not_run_on_its_held_lane(self):
        """Merge with lane_pick: once route() has scored a task, why_not_runnable must not wave a
        held lane through because fallback_lane() names a lane lane_pick already found
        unavailable (here: claude over the spend cap)."""
        self.say("codex", 10, 41)
        self.R.C["daily_spend_cap_usd"] = 0.0
        self.add("held", lane="codex")
        t = self.R.route(self.R.load_queue(), self.R.load_state())[0]
        self.assertEqual(t.lane, "codex", "nowhere to go")
        self.assertIn("codex held: 10% weekly left", self.R.why_not_runnable(t, self.R.load_state()) or "")

    def test_a_held_lane_is_unavailable_to_the_autopilot(self):
        self.say("codex", 10, 41)
        av = self.R.lane_availability(self.R.load_state())
        self.assertIn("codex held", av.get("codex") or "")

    def test_a_bugfix_still_routes_to_bugfix_lane_while_it_is_held(self):
        """Merge with master's bug-fix routing: a bug fix asking for another lane still goes to
        bugfix_lane (lane-routed + route-why), even with that lane held by the plan."""
        self.say("claude", 5, 100)
        self.add("bug", lane="codex", kind="bugfix")
        t = self.R.route(self.R.load_queue(), self.R.load_state())[0]
        self.assertEqual(t.lane, self.R.C["bugfix_lane"])
        self.assertEqual(t.fields.get("lane-routed"), "codex")
        self.assertIsNone(self.R.why_not_runnable(t, self.R.load_state()))

    def test_lane_pick_takes_the_plans_holds_but_not_for_a_bugfix_on_bugfix_lane(self):
        import lane_pick as LP
        live = LP.Live(now=self.R.now(), lanes=["claude", "codex"], held={"claude": "claude held: 5% left"})
        cfg = {**self.R.C, "lane_fallback": {"claude": "codex"}}
        work = LP.choose(LP.Ask(slug="w", asked="claude", work="code-edit"), cfg, live)
        self.assertEqual((work.lane, work.moved), ("codex", "fallback"))
        bug = LP.choose(LP.Ask(slug="b", asked="claude", work="debug", bugfix=True), cfg, live)
        self.assertEqual(bug.lane, "claude")

    def test_autopilot_prefers_a_spend_down_lane(self):
        self.say("codex", 40, 2)
        prefer = self.R.spend_down_lanes(self.R.load_state())
        self.assertIn("codex", prefer)
        sys.path.insert(0, str(HERE.parent))
        from atrium import autopilot
        cfg = {**self.R.C, "autopilot_lanes": ["claude", "codex"]}
        lane, why = autopilot.choose_lane(False, {"claude": None, "codex": None}, {"codex": 5}, cfg, prefer)
        self.assertEqual(lane, "codex")
        self.assertIn("use it or lose it", why)
        self.assertEqual(autopilot.choose_lane(True, {"claude": None, "codex": None}, {}, cfg, prefer)[0], "claude")


if __name__ == "__main__":
    unittest.main()
