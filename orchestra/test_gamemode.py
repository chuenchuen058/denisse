"""Tests for orchestra/gamemode.py and the places that honour it (the runner's tick, Don's
consults, the guard). A throwaway git repo, the fake lane from test_runner, and every process
function replaced: no real process is killed, no Ollama is called, no scheduled task changes.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_gamemode -v
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
sys.path.insert(0, str(HERE / "hooks"))
import campaign  # noqa: E402
import gamemode  # noqa: E402
import guard  # noqa: E402
sys.path.append(str(HERE.parent))
import testing_git  # noqa: E402
import test_runner as TR  # noqa: E402  (its fake lane and task template; its tests are not re-run here)

os.environ["ORCHESTRA_LIVE_WINDOW"] = "false"
os.environ["ORCHESTRA_TOASTS"] = "false"

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 15, 21, 14, tzinfo=UTC)
LATER = dt.datetime(2026, 11, 15, 23, 40, tzinfo=UTC)


class Fakes:
    """Stand-ins for everything gamemode and campaign touch outside the repo, with a record."""

    def __init__(self, alive=(), models=("llama3.1:8b",), tasks=None, ollama_up=True):
        self.alive = set(alive)
        self.killed, self.unloaded, self.changed = [], [], []
        self.models = list(models)
        self.ollama_up = ollama_up
        # scheduled task -> enabled (None: not installed)
        self.tasks = dict(tasks if tasks is not None else {n: True for n in gamemode.CFG["scheduled_tasks"]})
        self.stopped = self.started = 0

    def patches(self):
        return [mock.patch.object(campaign, "proc_alive", lambda pid, created=None: (pid, created) in self.alive),
                mock.patch.object(campaign, "kill_tree_pid", self.killed.append),
                mock.patch.object(gamemode, "loaded_models", lambda: (self.ollama_up, list(self.models))),
                mock.patch.object(gamemode, "unload_model", self._unload),
                mock.patch.object(gamemode, "task_enabled", lambda name: self.tasks.get(name)),
                mock.patch.object(gamemode, "set_task", self._set_task),
                mock.patch.object(gamemode, "stop_ollama", self._stop),
                mock.patch.object(gamemode, "start_ollama", self._start),
                mock.patch.object(gamemode, "_refresh_state_now", lambda: None)]

    def _unload(self, m):
        self.unloaded.append(m)
        self.models.remove(m)

    def _set_task(self, name, enabled):
        self.changed.append((name, enabled))
        self.tasks[name] = enabled
        return True

    def _stop(self):
        self.stopped += 1
        return True

    def _start(self):
        self.started += 1
        return True


class GameModeTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)  # noqa: E731
        g("init", "-q", "-b", "main")
        g("config", "user.name", "t"); g("config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text(".review/*\n.venv\n")
        (self.repo / "orchestra").mkdir()
        shutil.copy(HERE / "review_prompt.md", self.repo / "orchestra" / "review_prompt.md")
        g("add", "."); g("commit", "-qm", "init")
        (self.repo / ".review").mkdir()
        (self.repo / ".review" / "lanes.json").write_text(json.dumps({"fake": [sys.executable, "-c", TR.FAKE]}))
        keys = ("ORCHESTRA_REPO", "ORCHESTRA_BACKOFF_MIN", "ORCHESTRA_TIMEOUT_S", "ORCHESTRA_POWER", *TR.FAKE_ENV)
        self.saved_env = {k: os.environ.get(k) for k in keys}
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_BACKOFF_MIN"] = "0"
        os.environ["ORCHESTRA_TIMEOUT_S"] = json.dumps({"light": 10, "heavy": 10})
        os.environ["ORCHESTRA_POWER"] = "ac"
        for k in TR.FAKE_ENV:
            os.environ.pop(k, None)
        import runner
        self.R = importlib.reload(runner)
        self.review = self.R.REVIEW

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for wt in (self.R.WORKTREES.glob("*") if self.R.WORKTREES.exists() else []):
            self.R.unlink_link(wt / ".venv")
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers
    def add(self, slug, folder=None, extra=""):
        p = (folder or self.R.QUEUE) / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TR.TASK.format(title=slug, queue="auto", priority="normal", kind="work",
                                    written=dt.datetime.now().strftime("%Y-%m-%dT%H:%M"),
                                    gate=TR.GATE_OK, extra=extra))
        return p

    def running(self, slug, lock="runner.fake.lock", pid=4101, lane_pid=4102):
        self.add(slug)
        subprocess.run(["git", "branch", f"review/{slug}"], cwd=self.repo, check=True, capture_output=True)
        (self.review / lock).write_text(json.dumps(
            {"pid": pid, "created": 11, "lane_pid": lane_pid, "lane_created": 22, "task": slug,
             "since": "2026-11-15T21:00:00"}))
        return {(pid, 11), (lane_pid, 22)}

    def switch(self, fakes, fn, now):
        ps = fakes.patches()
        for p in ps:
            p.start()
        try:
            return fn(self.R, now)
        finally:
            for p in reversed(ps):
                p.stop()

    # -- on
    def test_on_pauses_parks_running_unloads_and_records(self):
        alive = self.running("busy") | self.running("busy2", "runner.fake.2.lock", 4201, 4202)
        self.add("waiting", extra=" 2")
        f = Fakes(alive=alive | {(9999, 33)}, tasks={"Denisse Orchestra": True, "Denisse Eval": False})
        st = self.switch(f, gamemode.on, NOW)
        self.assertTrue(st["on"])
        self.assertEqual(sorted(f.killed), [4101, 4102, 4201, 4202])  # only the lock-named trees
        self.assertEqual(campaign.dispatch_block(self.review), campaign.PAUSED)
        for slug in ("busy", "busy2"):
            self.assertFalse((self.R.QUEUE / f"{slug}.md").exists())
            why = (self.R.PARKED / f"{slug}.why.txt").read_text(encoding="utf-8")
            self.assertIn(gamemode.CFG["paused_status"], why)
            self.assertIn(f"review/{slug}", why)
            self.assertEqual(self.R.load_state()["tasks"][slug]["last_result"], gamemode.CFG["paused_result"])
        self.assertIn("review/busy", subprocess.run(["git", "branch", "--list", "review/busy"], cwd=self.repo,
                                                    capture_output=True, text=True).stdout)  # branch kept
        self.assertTrue((self.R.QUEUE / "waiting.md").exists())  # not running: untouched
        self.assertEqual(f.unloaded, ["llama3.1:8b"])
        self.assertEqual(f.stopped, 0)  # stop_ollama_server is off as shipped
        self.assertEqual(f.changed, [("Denisse Orchestra", False)])  # installed and enabled only
        on_disk = gamemode.status(self.review)
        self.assertTrue(on_disk["on"])
        self.assertEqual(sorted(on_disk["paused"]), ["busy", "busy2"])
        self.assertFalse(on_disk["was_paused"])
        self.assertEqual(on_disk["tasks_disabled"], ["Denisse Orchestra"])
        self.assertEqual(on_disk["ollama"]["unloaded"], ["llama3.1:8b"])
        s = gamemode.summary(on_disk)
        self.assertEqual((s["on"], s["paused"], s["unloaded"]), (True, 2, True))

    def test_on_twice_and_off_twice_are_harmless(self):
        alive = self.running("busy")
        f = Fakes(alive=alive)
        self.switch(f, gamemode.on, NOW)
        first = gamemode.status(self.review)
        again = self.switch(f, gamemode.on, LATER)
        self.assertEqual(gamemode.status(self.review), first)  # the record of what ON did is not overwritten
        self.assertIn("already on", again["lines"][0])
        self.assertEqual(f.killed, [4102, 4101])
        self.assertEqual(f.unloaded, ["llama3.1:8b"])
        self.switch(f, gamemode.off, LATER)
        changed = list(f.changed)
        out = self.switch(f, gamemode.off, LATER)
        self.assertIn("already off", out["lines"][0])
        self.assertEqual(f.changed, changed)
        self.assertTrue((self.R.QUEUE / "busy.md").exists())

    # -- off
    def test_off_resumes_and_requeues_exactly_those_and_reenables_exactly_those(self):
        alive = self.running("busy")
        self.add("parked-before", folder=self.R.PARKED)  # parked by something else: stays parked
        (self.R.PARKED / "parked-before.why.txt").write_text("gate failed\n")
        f = Fakes(alive=alive, tasks={"Denisse Orchestra": True, "Denisse Atrium Digest": True, "Denisse Eval": False})
        self.switch(f, gamemode.on, NOW)
        st = self.switch(f, gamemode.off, LATER)
        self.assertFalse(st["on"])
        self.assertIsNone(campaign.dispatch_block(self.review))
        queued = (self.R.QUEUE / "busy.md").read_text(encoding="utf-8")
        self.assertIn("interrupted by game mode", queued)  # the requeue note says so
        self.assertFalse((self.R.PARKED / "busy.md").exists())
        self.assertTrue((self.R.PARKED / "parked-before.md").exists())
        self.assertFalse((self.R.QUEUE / "parked-before.md").exists())
        self.assertEqual(self.R.load_state()["tasks"]["busy"]["attempts"], 0)
        self.assertEqual(f.changed, [("Denisse Orchestra", False), ("Denisse Atrium Digest", False),
                                     ("Denisse Orchestra", True), ("Denisse Atrium Digest", True)])
        self.assertEqual(f.tasks["Denisse Eval"], False)  # was off before: left off
        self.assertFalse(gamemode.is_on(self.review))
        self.assertEqual(gamemode.status(self.review)["last"]["paused"], ["busy"])

    def test_off_leaves_chuens_own_pause_in_place(self):
        campaign.pause(self.review, NOW)
        f = Fakes()
        self.switch(f, gamemode.on, NOW)
        self.assertTrue(gamemode.status(self.review)["was_paused"])
        self.switch(f, gamemode.off, LATER)
        self.assertEqual(campaign.dispatch_block(self.review), campaign.PAUSED)

    def test_off_respects_a_campaign_deadline_like_resume(self):
        (self.review / "campaign.json").write_text(json.dumps({
            "schema": 1, "id": "sprint", "purpose": "t", "enabled": True, "started_at": "2026-11-14T20:00:00Z",
            "stop_at": "2026-11-15T22:00:00Z", "stop_at_source": "test", "timezone": "Europe/London",
            "overrides": {}, "restart_after_stop": False}))
        alive = self.running("busy")
        f = Fakes(alive=alive)
        self.switch(f, gamemode.on, NOW)
        st = self.switch(f, gamemode.off, LATER)  # after stop_at: resume refuses, so dispatch stays paused
        self.assertEqual(campaign.dispatch_block(self.review), campaign.PAUSED)
        self.assertTrue(any("stays paused" in line for line in st["lines"]))
        self.assertTrue((self.R.QUEUE / "busy.md").exists())  # still requeued, ready for --after-deadline
        self.assertFalse(gamemode.is_on(self.review))

    def test_ollama_server_is_stopped_only_when_config_says_and_restarted_by_off(self):
        f = Fakes()
        with mock.patch.dict(gamemode.CFG, stop_ollama_server=True):
            self.switch(f, gamemode.on, NOW)
            self.assertEqual(f.stopped, 1)
            self.assertTrue(gamemode.summary(gamemode.status(self.review))["stopped"])
            self.switch(f, gamemode.off, LATER)
        self.assertEqual(f.started, 1)
        g = Fakes(ollama_up=False, models=())
        with mock.patch.dict(gamemode.CFG, stop_ollama_server=True):
            self.switch(g, gamemode.on, NOW)
            self.switch(g, gamemode.off, LATER)
        self.assertEqual((g.stopped, g.started), (0, 0))  # it wasn't running: nothing stopped or started

    # -- the places that honour it
    def test_runner_tick_does_nothing_while_on(self):
        self.add("next")
        (self.review / gamemode.CFG["state_file"]).write_text(json.dumps({"on": True, "since": "2026-11-15T21:14:00Z"}))
        with mock.patch.object(self.R, "reconcile", side_effect=AssertionError("reconcile ran")), \
                mock.patch.object(self.R, "fresh_worktree", side_effect=AssertionError("a worktree was made")), \
                mock.patch.object(self.R.master_health, "step", side_effect=AssertionError("master health ran")), \
                mock.patch.object(self.R, "autopilot_step", side_effect=AssertionError("autopilot ran")), \
                mock.patch.object(self.R, "git", side_effect=AssertionError("git ran")):
            self.assertEqual(self.R.tick(), gamemode.CFG["tick_result"])
        self.assertTrue((self.R.QUEUE / "next.md").exists())
        (self.review / gamemode.CFG["state_file"]).write_text(json.dumps({"on": False}))
        self.assertEqual(self.R.tick(), "PASS")  # off again: the same task runs

    def test_consult_is_skipped_while_on(self):
        import consult
        p = self.add("study", extra=" x")
        task = self.R.parse_task(p)
        (self.review / gamemode.CFG["state_file"]).write_text(json.dumps({"on": True}))
        logged = []
        with mock.patch.object(consult, "opinion", side_effect=AssertionError("Don was asked")):
            self.assertEqual(consult.before_lane(task, self.tmp, self.repo, log=logged.append), "")
        self.assertTrue(any(gamemode.CFG["tick_result"] in line for line in logged))

    def test_status_is_off_without_a_readable_file(self):
        self.assertEqual(gamemode.status(self.review), {"on": False})
        (self.review / gamemode.CFG["state_file"]).write_text("{half")
        self.assertFalse(gamemode.is_on(self.review))


class GuardGameModeTest(unittest.TestCase):
    main = os.path.abspath("/x/GitHub/denisse")
    wt = os.path.abspath("/x/GitHub/denisse-wt/task-1")

    def d(self, tool, **inp):
        return guard.decide({"tool_name": tool, "tool_input": inp, "cwd": self.wt}, self.wt, self.main)

    def test_workers_cannot_switch_game_mode(self):
        for cmd in ("python -m orchestra.gamemode on", ".venv/Scripts/python.exe orchestra/gamemode.py off",
                    "echo {} > .review/gamemode.json"):
            self.assertIsNotNone(self.d("Bash", command=cmd), cmd)
        self.assertIsNotNone(self.d("Write", file_path=os.path.join(self.wt, ".review", "gamemode.json")))
        self.assertIsNone(self.d("Bash", command="python -m orchestra.gamemode status"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
