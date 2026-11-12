"""Tests for orchestra/campaign.py and the runner's use of it: fake clock, fake lane,
mocked process killing. No model calls, no real processes killed.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_campaign -v
"""
import datetime as dt
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "hooks"))
import campaign  # noqa: E402
import guard  # noqa: E402
sys.path.append(str(HERE.parent))  # testing_git; appended so it never shadows a local module
import testing_git  # noqa: E402
import test_runner as TR  # noqa: E402  (its fake lane and task template; its tests are not re-run here)
# Never a real window or toast from a test (orchestra/visible.py: the live window and toasts).
os.environ["ORCHESTRA_LIVE_WINDOW"] = "false"
os.environ["ORCHESTRA_TOASTS"] = "false"

UTC = dt.timezone.utc
START = dt.datetime(2026, 11, 2, 20, 0, tzinfo=UTC)
STOP = dt.datetime(2026, 11, 4, 18, 0, tzinfo=UTC)  # 18:00 Europe/London: GMT in November
BEFORE = dt.datetime(2026, 11, 2, 20, 0, tzinfo=UTC)
AFTER = dt.datetime(2026, 11, 4, 18, 0, 1, tzinfo=UTC)
CFG = {"lane_slots": {"claude": 1, "codex": 1}, "daily_spend_cap_usd": 5.0, "other": 7}


def sprint(**over):
    c = {"schema": 1, "id": "sprint", "purpose": "test", "enabled": True,
         "started_at": "2026-11-02T20:00:00Z", "stop_at": "2026-11-04T18:00:00Z",
         "stop_at_source": "Chuen in chat, 2 Nov", "timezone": "Europe/London",
         "overrides": {"lane_slots": {"claude": 3, "codex": 1}, "daily_spend_cap_usd": 400},
         "restart_after_stop": False}
    c.update(over)
    return c


class CampaignFileTest(unittest.TestCase):
    """campaign.json on its own: validation, overrides before/after stop_at."""

    def setUp(self):
        self.review = Path(tempfile.mkdtemp())
        self.logged = []

    def tearDown(self):
        shutil.rmtree(self.review, ignore_errors=True)

    def write(self, data):
        (self.review / "campaign.json").write_text(data if isinstance(data, str) else json.dumps(data))

    def eff(self, now):
        return campaign.apply(CFG, self.review, now, self.logged.append)

    def test_overrides_win_before_stop_at_and_end_after_it(self):
        self.write(sprint())
        e = self.eff(BEFORE)
        self.assertEqual(e["lane_slots"], {"claude": 3, "codex": 1})
        self.assertEqual(e["daily_spend_cap_usd"], 400.0)
        self.assertEqual(e["other"], 7)
        self.assertEqual(self.eff(STOP - dt.timedelta(seconds=1))["lane_slots"]["claude"], 3)
        for t in (STOP, AFTER, AFTER + dt.timedelta(days=30), START - dt.timedelta(seconds=1)):
            self.assertEqual(self.eff(t), CFG, t)
        self.assertEqual(self.logged, [])

    def test_disabled_campaign_applies_nothing(self):
        self.write(sprint(enabled=False))
        self.assertEqual(self.eff(BEFORE), CFG)

    def test_no_campaign_is_config_json(self):
        self.assertEqual(self.eff(BEFORE), CFG)

    def test_malformed_campaign_is_ignored_whole_and_logged(self):
        bad = [
            "{not json",
            "[]",
            json.dumps(sprint(schema=2)),
            json.dumps(sprint(stop_at="2026-11-04T18:00")),  # no offset: zone is a guess
            json.dumps(sprint(stop_at="2026-11-01T18:00:00Z")),  # before started_at
            json.dumps(sprint(enabled="yes")),
            json.dumps(sprint(timezone="UTC")),
            json.dumps(sprint(extra_field=1)),
            # lane_slots fine, cap bad: neither may apply (never half a file)
            json.dumps(sprint(overrides={"lane_slots": {"claude": 3}, "daily_spend_cap_usd": "lots"})),
            json.dumps(sprint(overrides={"lane_slots": {"claude": 0}})),
            json.dumps(sprint(overrides={"timeout_s": {"heavy": 99999}})),  # not an overridable key
            json.dumps({k: v for k, v in sprint().items() if k != "stop_at_source"}),
        ]
        for text in bad:
            self.logged.clear()
            self.write(text)
            self.assertEqual(self.eff(BEFORE), CFG, text)
            self.assertEqual(len(self.logged), 1, text)
            self.assertIn("ignored", self.logged[0])

    def test_underscore_keys_are_comments(self):
        self.write(sprint(_note="why"))
        self.assertEqual(self.eff(BEFORE)["daily_spend_cap_usd"], 400.0)

    def test_tracked_seed_is_the_sprint_chuen_set(self):
        seed = json.loads((HERE / "campaign_seed.json").read_text(encoding="utf-8"))
        self.assertEqual(campaign.validate(seed), [])
        self.assertEqual(campaign.parse_utc(seed["stop_at"]), STOP)
        self.assertEqual(campaign.london(STOP).strftime("%a %d %b %H:%M"), "Wed 04 Nov 18:00")
        self.assertEqual(seed["overrides"], {"lane_slots": {"claude": 3, "codex": 1}, "daily_spend_cap_usd": 400})
        self.assertEqual(seed["stop_at_source"], "Chuen in chat, 2 Nov")
        self.assertFalse(seed["restart_after_stop"])
        cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        # gemini: 1 added by Chuen's request, 12 Nov 2026 (Gemini via the Antigravity CLI)
        self.assertEqual(cfg["lane_slots"], {"claude": 1, "codex": 1, "copilot": 1, "gemini": 1})
        self.assertEqual(cfg["daily_spend_cap_usd"], 5.0)

    def test_seed_installs_once_and_a_deleted_campaign_stays_deleted(self):
        repo = Path(tempfile.mkdtemp())
        try:
            (repo / "orchestra").mkdir()
            (repo / "orchestra" / "campaign_seed.json").write_text(json.dumps(sprint()))
            self.assertTrue(campaign.seed(self.review, repo))
            self.assertEqual(json.loads((self.review / "campaign.json").read_text())["id"], "sprint")
            self.assertFalse(campaign.seed(self.review, repo))  # already there
            (self.review / "campaign.json").unlink()
            self.assertFalse(campaign.seed(self.review, repo))  # Chuen deleted it: not resurrected
            self.assertFalse((self.review / "campaign.json").exists())
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_state_now_line(self):
        self.write(sprint())
        now = dt.datetime(2026, 11, 2, 20, 0, tzinfo=UTC)
        line = campaign.campaign_line(self.review, now)
        self.assertTrue(line.startswith("**Sprint:** ends Wed 18:00, 1 d 22 h left, Claude ×3, Codex ×1"), line)
        self.assertIn("ended", campaign.campaign_line(self.review, AFTER))
        campaign.pause(self.review, BEFORE)
        self.assertIn("paused by Chuen", campaign.campaign_line(self.review, now))


class ControlTest(unittest.TestCase):
    """control.json: pause / resume / stop, persistence, the deadline on resume."""

    def setUp(self):
        self.review = Path(tempfile.mkdtemp())
        (self.review / "campaign.json").write_text(json.dumps(sprint()))

    def tearDown(self):
        shutil.rmtree(self.review, ignore_errors=True)

    def test_pause_and_resume_within_the_deadline(self):
        self.assertIsNone(campaign.dispatch_block(self.review))
        campaign.pause(self.review, BEFORE)
        self.assertEqual(campaign.dispatch_block(self.review), "paused by Chuen")
        ok, msg = campaign.resume(self.review, BEFORE + dt.timedelta(hours=1))
        self.assertTrue(ok, msg)
        self.assertIsNone(campaign.dispatch_block(self.review))

    def test_resume_is_refused_after_the_deadline(self):
        campaign.pause(self.review, BEFORE)
        ok, msg = campaign.resume(self.review, AFTER)
        self.assertFalse(ok)
        self.assertIn("refused", msg)
        self.assertEqual(campaign.dispatch_block(self.review), "paused by Chuen")
        ok, msg = campaign.resume(self.review, AFTER, after_deadline=True)  # explicit, on config.json values
        self.assertTrue(ok, msg)
        self.assertIsNone(campaign.dispatch_block(self.review))
        self.assertEqual(campaign.apply(CFG, self.review, AFTER), CFG)

    def test_stop_ends_the_campaign_and_blocks_dispatch(self):
        campaign.stop(self.review, BEFORE)
        self.assertEqual(campaign.dispatch_block(self.review), "campaign ended")
        self.assertEqual(campaign.apply(CFG, self.review, BEFORE), CFG)  # overrides off at once
        ok, msg = campaign.resume(self.review, BEFORE + dt.timedelta(hours=1))
        self.assertTrue(ok, msg)
        self.assertIn("stays ended", msg)  # restart_after_stop: false
        self.assertIsNone(campaign.dispatch_block(self.review))
        self.assertEqual(campaign.apply(CFG, self.review, BEFORE), CFG)

    def test_restart_after_stop_true_brings_the_overrides_back_on_resume(self):
        (self.review / "campaign.json").write_text(json.dumps(sprint(restart_after_stop=True)))
        campaign.stop(self.review, BEFORE)
        campaign.resume(self.review, BEFORE + dt.timedelta(hours=1))
        self.assertEqual(campaign.apply(CFG, self.review, BEFORE)["daily_spend_cap_usd"], 400.0)

    def test_disable_schedule_holds_until_resume(self):
        campaign.disable_schedule(self.review, BEFORE)
        self.assertEqual(campaign.dispatch_block(self.review), "scheduled dispatch disabled by Chuen")
        campaign.resume(self.review, BEFORE)
        self.assertIsNone(campaign.dispatch_block(self.review))

    def test_no_campaign_resume_is_always_allowed(self):
        (self.review / "campaign.json").unlink()
        campaign.pause(self.review, AFTER)
        self.assertTrue(campaign.resume(self.review, AFTER)[0])


class RunnerCampaignTest(unittest.TestCase):
    """The runner honours the controls: a throwaway git repo and the fake lane from test_runner."""

    def setUp(self):
        testing_git.hermetic_git(self)  # first: no inherited GIT_* reaches a real repo
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        g("init", "-q", "-b", "main")
        g("config", "user.name", "t"); g("config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text(".review/*\n.venv\n")
        (self.repo / "orchestra").mkdir()
        shutil.copy(HERE / "review_prompt.md", self.repo / "orchestra" / "review_prompt.md")
        g("add", "."); g("commit", "-qm", "init")
        (self.repo / ".review").mkdir()
        (self.repo / ".review" / "lanes.json").write_text(json.dumps({"fake": [sys.executable, "-c", TR.FAKE]}))
        self.saved_env = {k: os.environ.get(k) for k in ("ORCHESTRA_REPO", "ORCHESTRA_BACKOFF_MIN",
                                                          "ORCHESTRA_DAILY_SPEND_CAP_USD", "ORCHESTRA_TIMEOUT_S",
                                                          "ORCHESTRA_POWER", *TR.FAKE_ENV)}
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_BACKOFF_MIN"] = "0"
        os.environ.pop("ORCHESTRA_DAILY_SPEND_CAP_USD", None)
        os.environ["ORCHESTRA_TIMEOUT_S"] = json.dumps({"light": 10, "heavy": 10})
        os.environ["ORCHESTRA_POWER"] = "ac"  # a tick on battery dispatches nothing; never depend on the plug
        for k in TR.FAKE_ENV:
            os.environ.pop(k, None)
        import runner
        self.R = importlib.reload(runner)
        self.real_utcnow = campaign.utcnow

    def tearDown(self):
        campaign.utcnow = self.real_utcnow
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for wt in (self.R.WORKTREES.glob("*") if self.R.WORKTREES.exists() else []):
            self.R.unlink_link(wt / ".venv")
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, slug, extra=""):
        p = self.R.QUEUE / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TR.TASK.format(title=slug, queue="auto", priority="normal", kind="work",
                                    written=dt.datetime.now().strftime("%Y-%m-%dT%H:%M"),
                                    gate=TR.GATE_OK, extra=extra))
        return p

    def why(self, slug):
        t = next(t for t in self.R.load_queue() if t.slug == slug)
        return self.R.why_not_runnable(t, self.R.load_state())

    def test_runner_config_follows_the_fake_clock(self):
        (self.R.REVIEW / "campaign.json").write_text(json.dumps(sprint()))
        self.assertEqual(self.R.load_config(now_utc=BEFORE)["lane_slots"]["claude"], 3)
        self.assertEqual(self.R.load_config(now_utc=BEFORE)["daily_spend_cap_usd"], 400.0)
        self.assertEqual(self.R.load_config(now_utc=AFTER)["lane_slots"]["claude"], 1)
        self.assertEqual(self.R.load_config(now_utc=AFTER)["daily_spend_cap_usd"], 5.0)
        # and a tick re-reads it: the cap in force follows the clock with no config edit
        campaign.utcnow = lambda: BEFORE
        self.R.tick()
        self.assertEqual(self.R.C["daily_spend_cap_usd"], 400.0)
        campaign.utcnow = lambda: AFTER
        self.R.tick()
        self.assertEqual(self.R.C["daily_spend_cap_usd"], 5.0)

    def test_malformed_campaign_never_crashes_a_tick(self):
        (self.R.REVIEW / "campaign.json").write_text("{half a file")
        self.add("m")
        self.assertEqual(self.R.tick(), "PASS")
        self.assertEqual(self.R.C["daily_spend_cap_usd"], 5.0)
        self.assertIn("campaign.json ignored", self.R.LOG_FILE.read_text(encoding="utf-8"))

    def test_pause_blocks_dispatch_but_not_a_running_task(self):
        self.add("running", extra=" 1")
        os.environ["FAKE_SLEEP"] = "2"
        os.environ["FAKE_COMMIT"] = "1"
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("r", self.R.tick()))
        th.start()
        for _ in range(100):  # wait until the lane has started (its pid is in the lock)
            try:
                if "lane_pid" in json.loads(self.R.LOCK_FILE.read_text()):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        campaign.pause(self.R.REVIEW)
        th.join(30)
        self.assertEqual(out.get("r"), "PASS")  # the running task finished and merged
        self.assertTrue((self.repo / "done.txt").exists())
        os.environ.pop("FAKE_SLEEP")
        self.add("next", extra=" 2")
        self.assertEqual(self.why("next"), "paused by Chuen")
        self.assertEqual(self.R.tick(), "idle")
        self.assertTrue((self.R.QUEUE / "next.md").exists())
        # nothing the runner does undoes a manual pause: more ticks, a fresh process, a reload
        importlib.reload(self.R)
        self.R.tick()
        self.assertEqual(self.why("next"), "paused by Chuen")
        campaign.resume(self.R.REVIEW)
        self.assertIsNone(self.why("next"))

    def test_stop_reason_is_campaign_ended(self):
        (self.R.REVIEW / "campaign.json").write_text(json.dumps(sprint()))
        self.add("s")
        campaign.stop(self.R.REVIEW, BEFORE)
        self.assertEqual(self.why("s"), "campaign ended")

    def test_emergency_stop_kills_only_the_owned_tree_and_parks_as_cancelled(self):
        self.add("busy")
        self.add("queued", extra=" 2")
        subprocess.run(["git", "branch", "review/busy"], cwd=self.repo, check=True, capture_output=True)
        (self.R.REVIEW / "runner.fake.lock").write_text(json.dumps(
            {"pid": 4101, "created": 11, "lane_pid": 4102, "lane_created": 22, "lane_job": "orchestra-lane-4101-1",
             "task": "busy", "since": "2026-11-02T20:05:00"}))
        alive = {(4101, 11), (4102, 22), (9999, 33)}  # 9999: an unrelated claude/python/ollama process
        killed, asked = [], []
        real_alive, real_kill = campaign.proc_alive, campaign.kill_tree_pid
        campaign.proc_alive = lambda pid, created=None: (asked.append((pid, created)), (pid, created) in alive)[1]
        campaign.kill_tree_pid = killed.append
        try:
            lines = campaign.emergency_stop(self.R, BEFORE)
        finally:
            campaign.proc_alive, campaign.kill_tree_pid = real_alive, real_kill
        self.assertEqual(killed, [4102, 4101])  # lane tree, then the tick waiting on it; nothing else
        self.assertEqual(asked, [(4101, 11), (4102, 22)], "each pid is checked with the lock's creation time")
        self.assertFalse((self.R.REVIEW / "runner.fake.lock").exists())
        self.assertFalse((self.R.QUEUE / "busy.md").exists())
        why = (self.R.PARKED / "busy.why.txt").read_text(encoding="utf-8")
        self.assertIn("cancelled by Chuen", why)
        self.assertIn("review/busy", why)
        branches = subprocess.run(["git", "branch", "--list", "review/busy"], cwd=self.repo,
                                  capture_output=True, text=True).stdout
        self.assertIn("review/busy", branches)  # branch kept
        self.assertTrue((self.R.QUEUE / "queued.md").exists())  # tasks not running are untouched
        self.assertEqual(self.R.load_state()["tasks"]["busy"]["last_result"], "CANCELLED")
        self.assertEqual(campaign.dispatch_block(self.R.REVIEW), "campaign ended")
        self.assertTrue(any("parked busy" in l for l in lines))

    def test_emergency_stop_leaves_a_lane_pid_alone_when_its_runner_is_gone(self):
        self.add("orphan")
        (self.R.REVIEW / "runner.fake.lock").write_text(json.dumps(
            {"pid": 4201, "lane_pid": 4202, "task": "orphan"}))
        killed = []
        real_alive, real_kill = campaign.proc_alive, campaign.kill_tree_pid
        campaign.proc_alive = lambda pid, created=None: pid == 4202  # runner dead; 4202 may now be anyone's
        campaign.kill_tree_pid = killed.append
        try:
            campaign.emergency_stop(self.R, BEFORE)
        finally:
            campaign.proc_alive, campaign.kill_tree_pid = real_alive, real_kill
        self.assertEqual(killed, [])
        self.assertTrue((self.R.PARKED / "orphan.why.txt").exists())

    def test_emergency_stop_spares_a_reused_pid(self):
        """The tick's pid is alive but with another creation time: it is not the runner any more."""
        self.add("reused")
        (self.R.REVIEW / "runner.fake.lock").write_text(json.dumps(
            {"pid": 4301, "created": 11, "lane_pid": 4302, "lane_created": 22, "task": "reused"}))
        killed = []
        real_alive, real_kill = campaign.proc_alive, campaign.kill_tree_pid
        # pid 4301 exists, but was created at 99, not the 11 the lock recorded
        campaign.proc_alive = lambda pid, created=None: (pid, created) in {(4301, 99), (4302, 22)}
        campaign.kill_tree_pid = killed.append
        try:
            campaign.emergency_stop(self.R, BEFORE)
        finally:
            campaign.proc_alive, campaign.kill_tree_pid = real_alive, real_kill
        self.assertEqual(killed, [])

    def test_proc_alive_is_the_runners_creation_time_rule(self):
        """Not mocked: campaign.proc_alive must delegate to runner._alive (the old helper it
        called no longer exists), with the same pid-reuse semantics."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        created = self.R.proc_created(child.pid)
        self.assertTrue(created, "own child's creation time must be readable")
        self.assertTrue(campaign.proc_alive(child.pid, created))
        self.assertTrue(campaign.proc_alive(child.pid, None), "a lock written before creation times: pid alone")
        self.assertFalse(campaign.proc_alive(child.pid, created + 1), "a reused pid is not the old worker")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.assertFalse(campaign.proc_alive(dead.pid, 1))

    def cli(self, *args):
        env = {**os.environ, "ORCHESTRA_REPO": str(self.repo)}
        return subprocess.run([sys.executable, str(HERE / "campaign.py"), *args], capture_output=True,
                              text=True, env=env, timeout=120)

    def test_cli_status_pause_resume_and_the_deadline(self):
        r = self.cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Campaign: none", r.stdout)
        self.assertIn("Dispatch: on", r.stdout)
        r = self.cli("pause")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(campaign.load_control(self.R.REVIEW)["paused"])
        self.assertIn("campaign.py pause", self.R.LOG_FILE.read_text(encoding="utf-8"))
        self.assertIn("paused by Chuen", (self.R.REVIEW / "STATE_NOW.md").read_text(encoding="utf-8"))
        self.assertIn("paused by Chuen", self.cli("status").stdout)
        # a campaign whose deadline has passed: resume is refused (exit 2) unless explicit
        (self.R.REVIEW / "campaign.json").write_text(json.dumps(sprint(
            started_at="2020-01-01T00:00:00Z", stop_at="2020-01-02T00:00:00Z")))
        r = self.cli("resume")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("refused", r.stdout)
        self.assertTrue(campaign.load_control(self.R.REVIEW)["paused"], "still paused")
        r = self.cli("resume", "--after-deadline")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("paused", campaign.load_control(self.R.REVIEW))
        r = self.cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-- ended", r.stdout)

    def test_lock_records_the_lane_pid(self):
        self.add("p")
        seen = []
        real = self.R.update_lock
        self.R.update_lock = lambda **extra: (seen.append(extra.get("lane_pid")), real(**extra))
        try:
            self.R.tick()
        finally:
            self.R.update_lock = real
        self.assertTrue(any(isinstance(p, int) and p > 0 for p in seen), seen)

    def test_status_command_shows_slots_and_pause(self):
        (self.R.REVIEW / "campaign.json").write_text(json.dumps(sprint()))
        (self.R.REVIEW / "runner.claude.2.lock").write_text(json.dumps({"pid": 1, "task": "t2", "since": "x"}))
        campaign.pause(self.R.REVIEW, BEFORE)
        text = campaign.status_text(self.R, BEFORE)
        self.assertIn("sprint (test) -- active", text)
        self.assertIn("1 d 22 h left", text)
        self.assertIn("claude 3: free", text)
        self.assertIn("claude 2: t2 since x", text)
        self.assertIn("paused by Chuen", text)


class GuardCampaignTest(unittest.TestCase):
    main = os.path.abspath("/x/GitHub/denisse")
    wt = os.path.abspath("/x/GitHub/denisse-wt/task-1")

    def d(self, tool, **inp):
        return guard.decide({"tool_name": tool, "tool_input": inp, "cwd": self.wt}, self.wt, self.main)

    def test_workers_cannot_touch_the_controls(self):
        for p in (os.path.join(self.wt, ".review", "campaign.json"), os.path.join(self.wt, ".review", "control.json"),
                  ".review/campaign.json", os.path.join(self.main, ".review", "control.json"),
                  os.path.join(self.wt, "orchestra", "campaign_seed.json")):
            self.assertIsNotNone(self.d("Write", file_path=p), p)
            self.assertIsNotNone(self.d("Edit", file_path=p), p)
        for cmd in ("echo {} > .review/campaign.json", "cp x.json .review/control.json",
                    "python -c \"open('.review/campaign.json','w').write('{}')\"",
                    ".venv/Scripts/python.exe orchestra/campaign.py resume",
                    "python orchestra\\campaign.py resume --after-deadline",
                    "python orchestra/campaign.py pause", "python orchestra/campaign.py disable-schedule"):
            self.assertIsNotNone(self.d("Bash", command=cmd), cmd)

    def test_reading_and_status_are_fine(self):
        self.assertIsNone(self.d("Write", file_path=os.path.join(self.wt, "orchestra", "campaign.py")))
        for cmd in ("cat .review/campaign.json", "python orchestra/campaign.py status",
                    ".venv/Scripts/python.exe -m unittest orchestra.test_campaign -v"):
            self.assertIsNone(self.d("Bash", command=cmd), cmd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
