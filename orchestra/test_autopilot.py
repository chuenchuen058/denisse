"""Tests for the autopilot (atrium/autopilot.py), the lane tabs (orchestra/lane_tab.py and
visible.lane_tab) and bug-fix routing. Fakes only: a fake queue and reports in a temp
folder, a fake terminal launcher (visible.SPAWN / WHICH replaced; a test that reached the
real ones would raise), fake power, a fake classifier; no window, no model, no network.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_autopilot -v
"""
# checks: slow  (the runner part builds real git repos and runs a fake lane)
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
import test_runner as TR  # noqa: E402  (also turns the live window and toasts off for the runner)
import visible  # noqa: E402
import lane_tab  # noqa: E402
from atrium import autopilot as AP  # noqa: E402

AT = dt.datetime(2026, 11, 9, 14, 0)
GATE = [f'"{sys.executable}" -c "pass"']


def shipped(name="config.json") -> dict:
    return {k: v for k, v in json.loads((HERE / name).read_text(encoding="utf-8")).items() if not k.startswith("_")}


def no_real(*a, **k):
    raise AssertionError("a test must never open a real window, tab or terminal")


@dataclass
class Task:
    slug: str = "2026-11-09T1300-fix-the-tray"
    title: str = "Fix the tray restart"
    lane: str = "claude"


# ---------------------------------------------------------------- config

class ConfigTest(unittest.TestCase):
    KEYS = [k for k in shipped("config_defaults.json")
            if k.startswith(("lane_tab", "autopilot", "bugfix_"))]

    def test_every_new_key_is_in_both_files_with_the_same_value(self):
        self.assertIn("lane_tabs", self.KEYS)
        self.assertIn("autopilot", self.KEYS)
        self.assertIn("bugfix_lane", self.KEYS)
        cfg, defaults = shipped(), shipped("config_defaults.json")
        for k in self.KEYS:
            self.assertEqual(cfg[k], defaults[k], k)

    def test_shipped_switches(self):
        cfg = shipped()
        self.assertIs(cfg["lane_tabs"], True)
        self.assertIs(cfg["autopilot"], True)
        self.assertEqual(cfg["bugfix_lane"], "claude")
        self.assertIn("claude", cfg["lane_tab_lanes"])
        self.assertEqual(cfg["autopilot_dedupe_days"], 14)

    def test_classify_prompt_and_body_format_with_their_fields(self):
        cfg = shipped()
        cfg["autopilot_classify_prompt"].format(source="s", origin="o", text="t", context="c")
        cfg["autopilot_body"].format(text="t", source="s", origin="o", context="c", why="w")
        cfg["lane_tab_done_text"].format(result="PASS", outcome="ran", linger=30)


# ---------------------------------------------------------------- the tab renderer

class RenderTest(unittest.TestCase):
    cfg = {**shipped(), "lane_tab_diff_lines": 3}

    def lines(self, ev):
        return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lane_tab.render(json.dumps(ev), self.cfg)))

    def test_assistant_text_and_tool_calls_with_their_target(self):
        out = self.lines({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reading the runner first."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "orchestra/runner.py"}}]}})
        self.assertIn("Reading the runner first.", out)
        self.assertIn("Bash(git status)", out)
        self.assertIn("Read(orchestra/runner.py)", out)

    def test_edit_shows_a_diff_and_cuts_long_ones(self):
        out = self.lines({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py", "old_string": "x = 1",
                                                            "new_string": "x = 2\ny = 3\nz = 4\nw = 5"}}]}})
        self.assertIn("Edit(a.py)", out)
        self.assertIn("- x = 1", out)
        self.assertIn("+ x = 2", out)
        self.assertIn("1 more lines", out)

    def test_tool_errors_and_results(self):
        out = self.lines({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "Traceback: boom"},
            {"type": "tool_result", "content": "line one\nline two"}]}})
        self.assertIn("Traceback: boom", out)
        self.assertIn("line one (+1 lines)", out)

    def test_result_init_plain_text_and_noise(self):
        self.assertIn("session finished", self.lines({"type": "result", "subtype": "success", "num_turns": 4,
                                                      "duration_ms": 120000, "total_cost_usd": 1.5}))
        self.assertIn("$1.50", self.lines({"type": "result", "total_cost_usd": 1.5}))
        self.assertIn("llama", self.lines({"type": "system", "subtype": "init", "model": "llama"}))
        self.assertEqual(lane_tab.render("codex says hello", self.cfg), ["codex says hello"])
        self.assertEqual(lane_tab.render("{not json", self.cfg), ["{not json"])
        self.assertEqual(lane_tab.render(json.dumps({"type": "stream_event"}), self.cfg), [])


class FollowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lanetab-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log, self.done = self.tmp / "x.lane.log", self.tmp / "x.lane.done"
        self.cfg = shipped()

    def test_follows_the_log_until_done_then_says_done(self):
        self.log.write_text("", encoding="utf-8")
        steps = [
            lambda: self.log.write_text('{"type": "assistant", "message": {"content": [{"type": "text", "text": "hel', encoding="utf-8"),
            lambda: self.log.open("a", encoding="utf-8").write('lo"}]}}\nplain line\n'),
            lambda: visible.lane_tab_done(self.done, "ran", None),
        ]
        out, slept = [], []

        def sleep(s):
            slept.append(s)
            if steps:
                steps.pop(0)()

        self.assertEqual(lane_tab.follow(self.log, self.done, self.cfg, max_s=999, linger=7, out=out.append,
                                         sleep=sleep, clock=lambda: 0), "done")
        text = "\n".join(out)
        self.assertIn("hello", text)  # a line split across two reads is rendered once, whole
        self.assertEqual(text.count("hello"), 1)
        self.assertIn("plain line", text)
        self.assertIn("DONE (ran)", text)
        self.assertEqual(slept[-1], 7)  # lingers, then the tab closes

    def test_stops_by_itself_without_a_done(self):
        t = iter(range(0, 10_000, 100))
        out = []
        self.assertEqual(lane_tab.follow(self.log, self.done, self.cfg, max_s=250, linger=0, out=out.append,
                                         sleep=lambda s: None, clock=lambda: next(t)), "max")
        self.assertIn("never said DONE", "\n".join(out))


# ---------------------------------------------------------------- the launcher

class LaneTabTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lanetab-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        guard = mock.patch.multiple(visible, SPAWN=no_real, WHICH=no_real)
        guard.start()
        self.addCleanup(guard.stop)
        self.cfg, self.logs, self.spawned = shipped(), [], []
        self.log_path, self.done = self.tmp / "s.lane.log", self.tmp / "s.lane.done"

    def spawn(self, argv, env):
        self.spawned.append(argv)
        return 4242

    def tab(self, task=None, which=lambda exe: "C:/wt.exe", **cfg):
        return visible.lane_tab(task or Task(), {**self.cfg, **cfg}, log_path=self.log_path, done_path=self.done,
                                timeout=1200, log=self.logs.append, spawn=self.spawn, which=which)

    def test_opens_one_titled_tab_following_the_lane_log(self):
        self.done.write_text("{}")  # an earlier attempt's DONE must not close the new tab at once
        self.assertEqual(self.tab(), "opened")
        argv = self.spawned[0]
        self.assertEqual(argv[0], "wt.exe")
        self.assertIn("Claude: Fix the tray restart", argv)
        self.assertIn(str(self.log_path), argv)
        self.assertIn(str(self.done), argv)
        self.assertIn(str(1200 + self.cfg["lane_tab_extra_s"]), argv)
        self.assertFalse(self.done.exists())
        self.assertIn("LANE tab opened", self.logs[0])

    def test_off_switch_and_lanes_not_listed(self):
        self.assertEqual(self.tab(lane_tabs=False), "off")
        self.assertEqual(self.tab(Task(lane="gemini")), "off")
        self.assertEqual(self.spawned, [])

    def test_codex_and_copilot_get_tabs_too(self):
        self.assertEqual(self.tab(Task(lane="codex")), "opened")
        self.assertIn("Codex: Fix the tray restart", self.spawned[0])

    def test_no_windows_terminal_falls_back_to_a_console_or_nothing(self):
        self.assertEqual(self.tab(which=lambda exe: None), "opened")
        self.assertNotEqual(self.spawned[0][0], "wt.exe")
        self.assertIn(str(HERE / "lane_tab.py"), self.spawned[0])
        self.assertEqual(self.tab(which=lambda exe: None, lane_tab_fallback_console=False), "failed")
        self.assertEqual(len(self.spawned), 1)

    def test_title_is_safe_for_a_wt_command_line_and_cut(self):
        t = visible.tab_title(Task(title='a; b "c" ' + "x" * 200), self.cfg)
        self.assertNotIn(";", t)
        self.assertNotIn('"', t)
        self.assertLessEqual(len(t), self.cfg["lane_tab_title_max"])

    def test_a_failing_launcher_never_raises(self):
        def boom(argv, env):
            raise OSError("no terminal")
        r = visible.lane_tab(Task(), self.cfg, log_path=self.log_path, done_path=self.done, timeout=1,
                             log=self.logs.append, spawn=boom, which=lambda e: "wt")
        self.assertEqual(r, "failed")
        self.assertIn("no terminal", self.logs[-1])

    def test_done_file_carries_the_reports_result(self):
        report = self.tmp / "r.md"
        report.write_text("RESULT: PARTIAL\nGATE: x\n", encoding="utf-8")
        visible.lane_tab_done(self.done, "ran", report)
        self.assertEqual(json.loads(self.done.read_text()), {"outcome": "ran", "result": "PARTIAL"})
        visible.lane_tab_done(self.done, "TIMEOUT", self.tmp / "missing.md")
        self.assertEqual(json.loads(self.done.read_text())["result"], "TIMEOUT")


# ---------------------------------------------------------------- the planner

class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="autopilot-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.review, self.vault, self.repo = self.tmp / "repo" / ".review", self.tmp / "vault", self.tmp / "repo"
        self.review.mkdir(parents=True)
        shutil.copy(ROOT / ".review" / "TEMPLATE.md", self.review / "TEMPLATE.md")
        (self.vault / "Atlas").mkdir(parents=True)
        self.cfg = {**shipped(), "autopilot_gate": GATE, "autopilot_sources": ["inbox"]}
        self.logs, self.calls = [], []
        self.state = {"tasks": {}, "lanes": {}}
        self.available = {"claude": None, "codex": None, "copilot": None}
        self.answers = {}  # text -> classifier answer (default: actionable, repo-only)

    def inbox(self, *lines, extra=""):
        (self.vault / "Atlas" / "Task Ideas.md").write_text(
            "# Task Ideas\n\n## Inbox\n" + "".join(f"- {l}\n" for l in lines) + "\n## Later\n- not this one\n" + extra,
            encoding="utf-8")

    def classify(self, c, cfg):
        self.calls.append(c)
        a = {"actionable": True, "repo_only": True, "bugfix": False, "title": "", "files": [], "why": "fake"}
        a.update(self.answers.get(c.text, {}))
        if isinstance(a.get("raise"), Exception):
            raise a["raise"]
        return a

    def refill(self, power=None, busy=None, files_of=None, **cfg):
        return AP.refill(cfg={**self.cfg, **cfg}, review=self.review, repo=self.repo, vault=self.vault,
                         state=self.state, available=self.available, power=power or {"ac": True}, busy=busy or {},
                         files_of=files_of or (lambda text: set()), log=self.logs.append, classify=self.classify,
                         at=AT)

    def header(self, path, key):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.startswith(key + ":"):
                return line.split(":", 1)[1].strip()
        return None

    def test_inbox_lines_become_tasks_through_new_task(self):
        self.inbox("Make the tray show the model name", "[x] already done thing")
        made = self.refill()
        self.assertEqual(len(made), 1)
        text = made[0].read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Make the tray show the model name"))
        self.assertEqual(self.header(made[0], "Queue"), "auto")
        self.assertEqual(self.header(made[0], "From"), "autopilot")
        self.assertIn(GATE[0], text)
        self.assertNotIn("not this one", "".join(c.text for c in self.calls))
        self.assertTrue(any(l.startswith("AUTOPILOT created") for l in self.logs))

    def test_not_repo_only_or_no_runnable_gate_stays_a_draft(self):
        self.inbox("Email the module lead about week 5", "Fix the tray")
        self.answers["Email the module lead about week 5"] = {"repo_only": False}
        made = self.refill()
        self.assertEqual([self.header(p, "Queue") for p in made], ["draft", "auto"])
        self.assertIn("draft: not shown to be repo-only", " ".join(self.logs))
        for p in made:
            p.unlink()
        self.review.joinpath("autopilot.json").unlink()
        made = self.refill(autopilot_gate=["no-such-program-xyz --fast"])
        self.assertEqual({self.header(p, "Queue") for p in made}, {"draft"})

    def test_model_failure_is_the_fallback_rule_and_a_draft(self):
        self.inbox("Fix the tray")
        self.answers["Fix the tray"] = {"raise": OSError("connection refused")}
        made = self.refill()
        self.assertEqual(self.header(made[0], "Queue"), "draft")
        self.assertTrue(any("could not classify" in l for l in self.logs))

    def test_not_actionable_is_dropped_once(self):
        self.inbox("Just a thought about colours")
        self.answers["Just a thought about colours"] = {"actionable": False}
        self.assertEqual(self.refill(), [])
        self.assertTrue(any(l.startswith("AUTOPILOT dropped") for l in self.logs))
        self.calls.clear()
        self.refill()
        self.assertEqual(self.calls, [])  # decided: not asked again

    def test_caps_per_hour_then_one_skip_line(self):
        self.inbox("one", "two", "three")
        self.assertEqual(len(self.refill(autopilot_max_per_hour=2)), 2)
        self.logs.clear()
        self.assertEqual(self.refill(autopilot_max_per_hour=2), [])
        self.assertEqual(self.refill(autopilot_max_per_hour=2), [])
        skips = [l for l in self.logs if l.startswith("AUTOPILOT skip")]
        self.assertEqual(len(skips), 1, self.logs)  # the reason did not change: logged once
        self.assertIn("cap reached (hour", skips[0])

    def test_never_recreates_the_same_line_or_an_open_title(self):
        self.inbox("Fix the tray")
        self.assertEqual(len(self.refill()), 1)
        self.assertEqual(self.refill(), [])  # same line already decided
        self.review.joinpath("autopilot.json").unlink()
        self.assertEqual(self.refill(), [])  # ledger gone: the open task's title still stops it
        self.assertTrue(any("not re-created" in l for l in self.logs))

    def test_battery_and_no_allowance_skip(self):
        self.inbox("Fix the tray")
        self.assertEqual(self.refill(power={"ac": False}), [])
        self.assertIn("AUTOPILOT skip: on battery", self.logs)
        self.available = {"claude": "limited until 15:00", "codex": "limited until 16:00", "copilot": "not installed"}
        self.assertEqual(self.refill(), [])
        self.assertIn("no lane has allowance", self.logs[-1])
        self.assertEqual(self.calls, [])

    def test_work_is_spread_over_lanes_with_the_fewest_recent_runs(self):
        end = (AT - dt.timedelta(hours=1)).isoformat()
        self.state["tasks"] = {"a": {"history": [{"lane": "claude", "end": end}, {"lane": "claude", "end": end},
                                                 {"lane": "codex", "end": end}]}}
        self.inbox("first", "second", "third")
        made = self.refill(autopilot_max_per_hour=5)
        self.assertEqual([self.header(p, "Lane") for p in made], ["copilot", "codex", "copilot"])
        self.available["copilot"] = "limited until 15:00"
        self.review.joinpath("autopilot.json").unlink()
        for p in made:
            p.unlink()
        # claude 2, codex 1: codex, then a tie (2-2) goes to the first in config order, claude
        self.assertEqual([self.header(p, "Lane") for p in self.refill(autopilot_max_per_hour=5)],
                         ["codex", "claude", "codex"])

    def test_a_bug_fix_goes_to_claude_whatever_the_balance_says(self):
        end = (AT - dt.timedelta(hours=1)).isoformat()
        self.state["tasks"] = {"a": {"history": [{"lane": "claude", "end": end}] * 5}}
        self.inbox("The tray crashes on start with a traceback")
        self.answers["The tray crashes on start with a traceback"] = {"bugfix": True}
        made = self.refill()
        self.assertEqual(self.header(made[0], "Lane"), "claude")
        self.assertEqual(self.header(made[0], "Kind"), "bugfix")
        self.assertTrue(any("claude: bug fix" in l for l in self.logs))

    def test_a_bug_fix_falls_back_when_claude_is_limited(self):
        self.available["claude"] = "limited until 15:00"
        self.inbox("The tray crashes")
        self.answers["The tray crashes"] = {"bugfix": True}
        made = self.refill()
        self.assertEqual(self.header(made[0], "Lane"), "codex")
        self.assertTrue(any("a bug fix, but claude is limited" in l for l in self.logs))

    def test_never_two_tasks_on_the_same_files(self):
        self.inbox("Change runner.py timeouts", "Change runner.py logging", "Touch tray.py")
        files = {"Change runner.py timeouts": ["orchestra/runner.py"], "Change runner.py logging": ["orchestra/runner.py"],
                 "Touch tray.py": ["tray.py"]}
        for k, v in files.items():
            self.answers[k] = {"files": v}
        made = self.refill(busy={"2026-11-09T1000-running": {"tray.py"}})
        self.assertEqual([p.read_text(encoding="utf-8").splitlines()[0] for p in made], ["# Change runner.py timeouts"])
        held = [l for l in self.logs if l.startswith("AUTOPILOT held")]
        self.assertEqual(len(held), 2)
        self.logs.clear()
        asked = len(self.calls)
        self.refill(busy={"2026-11-09T1000-running": {"tray.py"}, made[0].stem: {"orchestra/runner.py"}})
        self.assertFalse([l for l in self.logs if l.startswith("AUTOPILOT held")])  # same reason: not logged again
        self.assertEqual(len(self.calls), asked)  # a held one keeps its reading: the model isn't asked again

    def test_files_named_in_the_text_count_too(self):
        self.inbox("Edit tray.py")
        made = self.refill(busy={"x": {"tray.py"}}, files_of=lambda text: {"tray.py"} if "tray.py" in text else set())
        self.assertEqual(made, [])

    def test_report_follow_ups_and_bug_fixes_from_partial_reports(self):
        day = self.review / "archive" / AT.date().isoformat()
        day.mkdir(parents=True)
        (day / "2026-11-09T0900-a.report.md").write_text(
            "RESULT: PASS\nGATE: x\nHUMAN: none\nPREMISE: right\nDONE:\n- did it\nNOT DONE:\n- the README section\n"
            "ODD:\n- none\nCOMMITS: abc\n", encoding="utf-8")
        parked = self.review / "parked"
        parked.mkdir()
        (parked / "2026-11-08T0900-b.report.md").write_text(
            "RESULT: FAIL\nGATE: x\nNOT DONE:\n- the failing test in test_tray\nODD:\n- the gate took 40 min\n",
            encoding="utf-8")
        old = self.review / "archive" / "2026-10-01"
        old.mkdir()
        (old / "x.report.md").write_text("RESULT: FAIL\nNOT DONE:\n- ancient\n", encoding="utf-8")
        cands = AP.candidates(cfg={**self.cfg, "autopilot_sources": ["not_done", "odd"]}, review=self.review,
                              vault=None, state={}, at=AT)
        got = {(c.source, c.text, c.bugfix) for c in cands}
        self.assertEqual(got, {("not_done", "the README section", False),
                               ("not_done", "the failing test in test_tray", True),
                               ("odd", "the gate took 40 min", False)})

    def test_parked_tasks_but_not_those_waiting_on_chuen(self):
        parked = self.review / "parked"
        parked.mkdir()
        for slug, why, last in (("p1", "PARTIAL: a blind re-run won't change it", "PARTIAL"),
                                ("p2", "waiting on Chuen: sign in to KEATS", "PASS"),
                                ("p3", "integration: conflict in runner.py", "PASS")):
            (parked / f"{slug}.md").write_text(f"# Task {slug}\nQueue: auto\n", encoding="utf-8")
            (parked / f"{slug}.why.txt").write_text(f"2026-11-09T10:00:00 {why}\nTo run it again: ...\n", encoding="utf-8")
            self.state["tasks"][slug] = {"last_result": last}
        cands = AP.from_parked(self.review, self.state, self.cfg)
        self.assertEqual([(c.origin, c.bugfix) for c in cands], [("p1", True), ("p3", True)])
        self.assertTrue(cands[0].text.startswith("Task p1: PARTIAL"))

    def test_waiting_items_skip_orchestras_own_block(self):
        (self.vault / "hot.md").write_text(
            "# hot\n\n## Waiting on Chuen\n- Decide the tray icon colour\n<!-- orchestra:waiting -->\n"
            "- Parked \"x\": y\n<!-- /orchestra:waiting -->\n\n## Active right now\n- other\n", encoding="utf-8")
        cands = AP.from_waiting(self.vault, self.cfg)
        self.assertEqual([c.text for c in cands], ["Decide the tray icon colour"])

    def test_off_switch(self):
        self.inbox("Fix the tray")
        self.assertEqual(self.refill(autopilot=False), [])
        self.assertEqual(self.logs, [])

    def test_model_classify_reads_the_local_models_json(self):
        reply = {"message": {"content": json.dumps({"actionable": True, "repo_only": True, "bugfix": True,
                                                    "title": "Fix it", "files": ["tray.py"]})}}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(reply).encode()

        seen = {}

        def urlopen(req, timeout):
            seen["body"] = json.loads(req.data.decode())
            seen["url"], seen["timeout"] = req.full_url, timeout
            return Resp()

        with mock.patch("urllib.request.urlopen", urlopen):
            out = AP.model_classify(AP.Candidate("inbox", "n", "the tray crashes"), self.cfg)
        self.assertTrue(out["bugfix"])
        self.assertEqual(seen["url"], self.cfg["autopilot_model_url"])
        self.assertEqual(seen["body"]["model"], self.cfg["autopilot_model"])
        self.assertIn("the tray crashes", seen["body"]["messages"][0]["content"])
        self.assertEqual(seen["body"]["format"]["type"], "object")
        reply["message"]["content"] = json.dumps({"actionable": "yes"})
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(ValueError):
                AP.model_classify(AP.Candidate("inbox", "n", "x"), self.cfg)


# ---------------------------------------------------------------- the runner

class RunnerPartsTest(unittest.TestCase):
    """The runner side, in TR's throwaway git repo with its fake lane (no model calls)."""
    add, state = TR.RunnerTest.add, TR.RunnerTest.state

    def setUp(self):
        # TR's setUp sets ORCHESTRA_* env vars for its own module; put them back afterwards, or
        # test_config (which runs after this module) reads a 3 s timeout and 0 min backoff.
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        TR.RunnerTest.setUp(self)

    def tearDown(self):
        TR.RunnerTest.tearDown(self)

    def start_log(self):
        return [l for l in self.R.LOG_FILE.read_text(encoding="utf-8").splitlines() if " START " in l]

    def lanes(self):
        lanes = json.loads((self.repo / ".review" / "lanes.json").read_text())
        lanes["fake2"] = lanes["fake"]
        (self.repo / ".review" / "lanes.json").write_text(json.dumps(lanes))

    def test_a_bugfix_kind_is_a_valid_header_and_runs_on_bugfix_lane(self):
        self.lanes()
        self.R.C["bugfix_lane"] = "fake2"
        self.add("2026-11-09T1300-bug", kind="bugfix")
        self.assertIsNone(self.R.header_problem(self.R.load_queue()[0]))
        self.assertEqual(self.R.tick(), "PASS")
        self.assertIn("lane=fake2 (fake2: bug fix; header lane fake)", self.start_log()[-1])
        h = self.state()["tasks"]["2026-11-09T1300-bug"]["history"][-1]
        self.assertEqual(h["lane"], "fake2")
        self.assertEqual(h["spent"], "$0.25")

    def test_lane_auto_is_picked_at_dispatch_and_recorded(self):
        import contextlib
        import io
        self.lanes()
        # fake2 fits by far the best, so no real lane (claude, codex, ...) can win and run
        self.R.C["lane_strengths"] = {**self.R.C["lane_strengths"], "fake2": {self.R.C["lane_work_default"]: 9.0}}
        auto = self.R.C["lane_auto_word"]
        slug = "2026-11-11T1700-auto"
        self.add(slug, lane=auto)
        t = self.R.load_queue()[0]
        self.assertIsNone(self.R.header_problem(t))
        self.assertIsNone(self.R.why_not_runnable(t, self.R.load_state()))
        self.assertEqual(t.lane, "fake2")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.R.tick(dry_run=True)
        self.assertIn("-> fake2:", out.getvalue(), "dry-run prints the winner")
        self.assertRegex(out.getvalue(), r"fake2\s+score 9\.")
        self.assertEqual(self.R.tick(), "PASS")
        self.assertIn(f"PICK {slug} -> fake2: ", self.R.LOG_FILE.read_text(encoding="utf-8"))
        h = self.state()["tasks"][slug]["history"][-1]
        self.assertEqual((h["lane"], h["lane_asked"]), ("fake2", auto))
        self.assertIn("fake2", h["pick_why"])
        names = self.R.C["lane_pick_headers"]
        done = next(self.R.ARCHIVE.rglob(f"{slug}.md")).read_text(encoding="utf-8")
        self.assertIn(f"\n{names['picked']}: fake2\n", done)
        self.assertIn(f"\n{names['why']}: ", done)

    def test_a_bugfix_stays_put_when_the_bug_lane_is_limited_or_the_lane_is_fixed(self):
        self.lanes()
        self.R.C["bugfix_lane"] = "fake2"
        until = (dt.datetime.now() + dt.timedelta(hours=1)).isoformat(timespec="seconds")
        state = {"tasks": {}, "lanes": {"fake2": {"limited_until": until}}}
        self.add("2026-11-09T1300-bug", kind="bugfix")
        self.add("2026-11-09T1301-pinned", kind="bugfix", headers="Lane-Fixed: yes\n")
        tasks = self.R.route(self.R.load_queue(), state)
        self.assertEqual([t.lane for t in tasks], ["fake", "fake"])
        tasks = self.R.route(self.R.load_queue(), {"tasks": {}, "lanes": {}})
        self.assertEqual([t.lane for t in tasks], ["fake2", "fake"])
        self.add("2026-11-09T1302-work")
        self.assertEqual(self.R.route(self.R.load_queue()[2:], {"tasks": {}, "lanes": {}})[0].lane, "fake")

    def test_the_lane_gets_its_tab_and_the_runner_keeps_everything_else(self):
        spawned = []
        self.R.C["lane_tab_lanes"] = ["fake"]
        with mock.patch.object(self.R, "tabs_enabled", lambda: True), \
                mock.patch.multiple(visible, SPAWN=lambda argv, env: spawned.append(argv) or 99,
                                    WHICH=lambda exe: "C:/wt.exe"):
            self.add("2026-11-09T1300-seen", title="Watch me")
            self.assertEqual(self.R.tick(), "PASS")
        self.assertEqual(len(spawned), 1)
        self.assertIn("Fake: Watch me", spawned[0])
        done = json.loads((self.R.REPORTS / "2026-11-09T1300-seen.lane.done").read_text())
        self.assertEqual(done, {"outcome": "ran", "result": "PASS"})
        h = self.state()["tasks"]["2026-11-09T1300-seen"]["history"][-1]
        self.assertEqual(h["spent"], "$0.25")  # cost still read from the same lane log

    def test_no_tab_from_a_test_repo_by_default(self):
        with mock.patch.multiple(visible, SPAWN=no_real, WHICH=no_real):
            self.add("2026-11-09T1300-quiet")
            self.assertEqual(self.R.tick(), "PASS")
        self.assertFalse((self.R.REPORTS / "2026-11-09T1300-quiet.lane.done").exists())

    def test_autopilot_step_refills_only_an_empty_queue(self):
        shutil.copy(ROOT / ".review" / "TEMPLATE.md", self.R.REVIEW / "TEMPLATE.md")
        vault = self.tmp / "vault"
        (vault / "Atlas").mkdir(parents=True)
        (vault / "Atlas" / "Task Ideas.md").write_text("## Inbox\n- Add a tray tooltip\n", encoding="utf-8")
        self.R.C.update(vault_dir=str(vault), autopilot_lanes=["fake"], autopilot_sources=["inbox"],
                        autopilot_gate=GATE)
        seen = []

        def classify(c, cfg):
            seen.append(c.text)
            return {"actionable": True, "repo_only": True, "bugfix": False, "title": "", "files": []}

        self.add("2026-11-09T1200-queued")
        self.assertEqual(self.R.autopilot_step(self.R.load_state(), classify, lambda: {"ac": True}), [])
        self.assertEqual(seen, [])  # an auto task is still queued: no planning
        (self.R.QUEUE / "2026-11-09T1200-queued.md").unlink()
        made = self.R.autopilot_step(self.R.load_state(), classify, lambda: {"ac": True})
        self.assertEqual(len(made), 1)
        task = self.R.parse_task(made[0])
        self.assertTrue(task.auto)
        self.assertEqual(task.lane, "fake")
        self.assertIsNone(self.R.header_problem(task))
        log = self.R.LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("AUTOPILOT created", log)
        self.assertEqual(self.R.tick(), "PASS")  # and the runner runs what the planner wrote

    def test_autopilot_step_never_raises(self):
        with mock.patch.object(self.R, "busy_files", side_effect=RuntimeError("boom")):
            self.assertEqual(self.R.autopilot_step(self.R.load_state(), None, lambda: {"ac": True}), [])
        self.assertIn("AUTOPILOT failed: RuntimeError: boom", self.R.LOG_FILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
