"""Tests for orchestra/exams with fake agents and a temp vault (no model calls).

    .venv\\Scripts\\python.exe -m unittest discover -s orchestra/exams -p "test_*.py" -v
"""
import contextlib
import datetime as dt
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from orchestra.exams import core, lane_for, agents  # noqa: E402
from orchestra.exams import __main__ as cli  # noqa: E402
from orchestra.exams.core import Reply  # noqa: E402

FIXES = {
    "code-edit-1": "def sum_to(n):\n    return sum(range(n + 1))\n",
    "code-edit-2": "def is_leap(y):\n    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)\n",
    "code-edit-3": "def reverse_words(s):\n    return ' '.join(reversed(s.split()))\n",
}
REWRITES = {
    "writing-1": "From 4 May, exam season, the library will open 24 hours a day, thanks to a former student's donation.",
    "writing-2": "The robotics competition has moved to the sports centre and now starts at 10:30. The prize is still a 3D printer.",
    "writing-3": "A Leeds study of 1,200 teenagers found late-night phone use meant lower maths marks.",
}


def good_answer(exam):
    g = exam["grader"]
    if g["type"] == "test":
        return f"Here is the fix:\n```python\n{FIXES[exam['id']]}```\n"
    if g["type"] == "exact":
        return f"The frame shows it.\nANSWER: {g['answer'][0]}"
    if g["type"] == "numeric":
        return f"Working...\nANSWER: {g['answer']}"
    if g["type"] == "writing":
        return f"ANSWER:\n{REWRITES[exam['id']]}"
    if g["type"] == "json":
        return "```json\n" + json.dumps(g["answer"]) + "\n```"
    raise AssertionError(g["type"])


BANK = core.load_bank()
BY_ID = {e["id"]: e for e in BANK}


def good_agent(prompt, cwd, timeout):
    exam = next(e for e in BANK if e["prompt"] == prompt)
    return Reply(text=good_answer(exam))


def bad_agent(prompt, cwd, timeout):
    return Reply(text="ANSWER: 99999\n```json\n{\"x\": 1}\n```")


def crashing_agent(prompt, cwd, timeout):
    raise RuntimeError("agent fell over")


def sleeping_agent(prompt, cwd, timeout):  # a real process, so the timeout path really kills
    return core.run_proc([sys.executable, "-c", "import time; time.sleep(30)"], cwd=cwd, timeout=timeout)


def gone_agent(prompt, cwd, timeout):
    return Reply(unavailable="IneligibleTierError: This client is no longer supported")


def editing_agent(prompt, cwd, timeout):  # edits the file in place, like codex would
    for eid, fix in FIXES.items():
        target = BY_ID[eid]["grader"]["target"]
        if (cwd / target).exists():
            (cwd / target).write_text(fix, encoding="utf-8")
    return Reply(text="done")


def at(hours_ago=0):
    return core.now() - dt.timedelta(hours=hours_ago)


def row(ok, seconds=10, answered=True):
    return {"exam": "x", "ok": ok, "seconds": seconds, "answered": answered}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exams-test-"))
        self.scores_file = self.tmp / "scores.json"
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def quiet_run(self, agents_, exams, **kw):
        lines = []
        s = core.run(agents_, exams, scores_file=self.scores_file, log=lines.append, **kw)
        return s, lines


class TestBank(Base):
    def test_at_least_three_per_category_all_auto_graded(self):
        for cat in core.CATEGORIES:
            self.assertGreaterEqual(len(core.load_bank(cat)), 3, cat)
        ids = [e["id"] for e in BANK]
        self.assertEqual(len(ids), len(set(ids)))
        for e in BANK:
            self.assertIn(e["grader"]["type"], ("test", "exact", "numeric", "writing", "json"), e["id"])
        self.assertEqual(sum(1 for e in BANK if e.get("canary")), 1)

    def test_every_exam_passes_its_reference_answer_and_fails_a_wrong_one(self):
        for e in BANK:
            with self.subTest(e["id"]):
                good = core.run_exam(good_agent, e)
                self.assertTrue(good["ok"], good)
                bad = core.run_exam(bad_agent, e)
                self.assertFalse(bad["ok"], bad)

    def test_code_edit_in_place_passes_and_unfixed_file_fails(self):
        for e in core.load_bank("code-edit"):
            self.assertTrue(core.run_exam(editing_agent, e)["ok"], e["id"])
            self.assertFalse(core.run_exam(lambda p, c, t: Reply(text="no idea"), e)["ok"])

    def test_writing_grader_counts_words_and_facts(self):
        e = BY_ID["writing-3"]
        long = "ANSWER:\n" + " ".join(["word"] * 30) + " Leeds 1,200 maths"
        ok, why = core.grade(e, long, self.tmp)
        self.assertFalse(ok)
        self.assertIn("words >", why)
        ok, why = core.grade(e, "ANSWER:\nA Leeds study of teenagers and maths.", self.tmp)
        self.assertFalse(ok)
        self.assertIn("1,200", why)

    def test_numeric_tolerance(self):
        e = BY_ID["study-1"]
        self.assertTrue(core.grade(e, "**ANSWER:** 1.2735", self.tmp)[0])
        self.assertTrue(core.grade(e, "ANSWER: 4/pi = 1.2732", self.tmp)[0])
        self.assertFalse(core.grade(e, "ANSWER: 1.30", self.tmp)[0])
        self.assertFalse(core.grade(e, "about 1.27", self.tmp)[0])


class TestRun(Base):
    def test_fails_and_timeouts_score_a_fail_and_never_stop_the_run(self):
        exams = [BY_ID["debug-1"], BY_ID["extract-1"]]
        scores, lines = self.quiet_run({"crash": crashing_agent, "slow": sleeping_agent, "good": good_agent},
                                       exams, timeout=1)
        a = scores["agents"]
        self.assertEqual([r["ok"] for r in a["crash"]["debug"]], [False])
        self.assertIn("agent fell over", a["crash"]["debug"][0]["error"])
        self.assertIn("timed out", a["slow"]["extract"][0]["error"])
        self.assertFalse(a["slow"]["extract"][0]["answered"])
        self.assertTrue(a["good"]["debug"][0]["ok"] and a["good"]["extract"][0]["ok"])
        self.assertEqual(len(lines), 6)
        self.assertEqual(json.loads(self.scores_file.read_text(encoding="utf-8"))["agents"].keys(), a.keys())

    def test_an_agent_that_cannot_take_work_is_skipped_not_scored(self):
        scores, lines = self.quiet_run({"gemini": gone_agent}, BANK)
        self.assertNotIn("gemini", scores["agents"])
        self.assertEqual(len(lines), 1)
        self.assertIn("SKIPPED", lines[0])
        self.assertIn("can't take work", core.not_working(scores)["gemini"])
        # a codex that passed before and is now refused (usage limit) is not routed to
        core.record(scores, "codex", "debug", row(True), when=at(hours_ago=2))
        scores["skipped"]["codex"] = {"at": at(hours_ago=1).isoformat(), "why": "usage limit"}
        self.assertIsNone(core.best(scores, "debug"))
        # once it answers again, it is back
        core.record(scores, "codex", "debug", row(True))
        self.assertEqual(core.best(scores, "debug")["agent"], "codex")

    def test_rolling_last_20(self):
        s = {"agents": {}}
        for i in range(25):
            core.record(s, "codex", "debug", row(i % 2 == 0, seconds=i))
        rows = s["agents"]["codex"]["debug"]
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0]["seconds"], 5)

    def test_ollama_not_running_means_no_local_agents(self):
        with mock.patch.object(agents, "OLLAMA", "http://127.0.0.1:9"):
            self.assertEqual(agents.ollama_models(timeout=1), [])
            found = agents.available()
            with_claude = agents.available(include_claude=True)  # inside the patch: no real Ollama
        self.assertEqual(sorted(found), ["codex", "copilot", "gemini"])  # claude only on request
        self.assertIn("claude", with_claude)

    def test_refusal_text_marks_an_agent_unavailable(self):
        r = agents._refused(Reply(text="IneligibleTierError: This client is no longer supported", error="exit 1"))
        self.assertTrue(r.unavailable)
        r = agents._refused(Reply(text="Traceback ... KeyError", error="exit 1"))
        self.assertFalse(r.unavailable)


def scores_with(**agents_rows):
    s = {"agents": {}}
    for name, cats in agents_rows.items():
        for cat, rows in cats.items():
            for i, r in enumerate(rows):
                core.record(s, name, cat, r, when=at(hours_ago=1) + dt.timedelta(seconds=i))
    return s


class TestRouter(Base):
    def test_highest_pass_rate_wins_ties_by_speed(self):
        s = scores_with(codex={"debug": [row(True, 40), row(True, 50)]},
                        copilot={"debug": [row(True, 10), row(True, 12)]},
                        **{"local:llama3.1:8b": {"debug": [row(True, 5), row(False, 5), row(True, 5)]}})
        self.assertEqual(core.best(s, "debug")["agent"], "copilot")
        self.assertIsNone(core.best(s, "study"))

    def test_not_working_is_never_chosen(self):
        s = scores_with(codex={"debug": [row(True, 5), row(False), row(False), row(False)]},
                        copilot={"debug": [row(True, 60), row(False, 60), row(True, 60)]})
        self.assertIn("codex", core.not_working(s))
        self.assertEqual(core.best(s, "debug")["agent"], "copilot")

    def test_silent_for_24h_is_not_working(self):
        s = {"agents": {}}
        core.record(s, "codex", "debug", row(True), when=at(hours_ago=30))
        core.record(s, "copilot", "debug", row(True), when=at(hours_ago=2))
        core.record(s, "copilot", "debug", row(False, answered=False), when=at(hours_ago=1))
        down = core.not_working(s)
        self.assertIn("codex", down)
        self.assertIn("no answer since", down["codex"])
        self.assertNotIn("copilot", down)

    def test_claude_only_when_nobody_else_passes(self):
        s = scores_with(claude={"writing": [row(True, 5)] * 3}, codex={"writing": [row(True, 90), row(False, 90), row(True, 9)]})
        self.assertEqual(core.best(s, "writing")["agent"], "codex")
        s = scores_with(claude={"writing": [row(True, 5)] * 3}, codex={"writing": [row(False), row(True), row(False)]})
        self.assertEqual(core.best(s, "writing")["agent"], "codex")
        s = scores_with(claude={"writing": [row(True, 5)] * 3}, codex={"study": [row(True)]})
        self.assertEqual(core.best(s, "writing")["agent"], "claude")
        self.assertEqual(core.best(s, "writing", among=["codex", "gemini"]), None)

    def test_cli_best_and_hub_kinds(self):
        s = scores_with(codex={"code-edit": [row(True)] * 3}, copilot={"code-edit": [row(False)] * 3})
        core.save_scores(s, self.scores_file)
        for arg in ("code-edit", "code_change"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["--scores", str(self.scores_file), "best", arg, "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out.getvalue())["agent"], "codex")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--scores", str(self.scores_file), "best", "study"])
        self.assertEqual(rc, 1)
        self.assertIn("none", out.getvalue())

    def test_lane_for(self):
        missing = self.tmp / "none.json"
        self.assertEqual(lane_for.lane_for("code-edit", scores_file=missing), "claude")
        s = scores_with(codex={"code-edit": [row(True)] * 3, "writing": [row(False)] * 3},
                        **{"local:llama3.1:8b": {"writing": [row(True)] * 3}})
        core.save_scores(s, self.scores_file)
        self.assertEqual(lane_for.lane_for("code_change", scores_file=self.scores_file), "codex")
        self.assertEqual(lane_for.lane_for(None, "Fix the tray restart bug", scores_file=self.scores_file), "codex")
        # the best writer is a local model, which is not an Orchestra lane: the old default
        self.assertEqual(lane_for.lane_for("writing", scores_file=self.scores_file), "claude")
        self.assertEqual(lane_for.lane_for(None, "Something vague", scores_file=self.scores_file), "claude")

    def test_lane_for_copilot_only_once_it_has_scores(self):
        self.assertIn("copilot", lane_for.orchestra_lanes(), "the router knows every lane in config.json")
        s = scores_with(codex={"code-edit": [row(True, 30)] * 3})
        core.save_scores(s, self.scores_file)
        self.assertEqual(lane_for.lane_for("code-edit", scores_file=self.scores_file), "codex",
                         "no copilot scores yet: never routed to copilot")
        s = scores_with(codex={"code-edit": [row(True, 30), row(False, 30)]},
                        copilot={"code-edit": [row(True, 10)] * 3})
        core.save_scores(s, self.scores_file)
        self.assertEqual(lane_for.lane_for("code-edit", scores_file=self.scores_file), "copilot")
        self.assertEqual(lane_for.lane_for("code-edit", scores_file=self.scores_file, lanes=("claude", "codex")),
                         "codex", "a lane the runner doesn't have is never picked")


class TestNoteAndCanary(Base):
    def test_note_in_temp_vault(self):
        vault = self.tmp / "vault"
        vault.mkdir()
        s = scores_with(codex={"debug": [row(True, 12)] * 3, "study": [row(False)]},
                        gemini={"debug": [row(False, answered=False)] * 3})
        note = core.write_note(s, vault)
        self.assertEqual(note, vault / "Dev" / "Agent-Scores.md")
        text = note.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: Agent-Scores\ndescription: "))
        for want in ("type: generated", "tags: [orchestra, agents]", f"updated: {core.now().date()}",
                     "| codex | - | 100% (3/3), 12s | - | 0% (0/1)","100% (3/3), 12s", "## Not working", "**gemini**: last 3"):
            self.assertIn(want, text)
        self.assertIn("- debug: **codex**", text)
        self.assertIsNone(core.write_note(s, self.tmp / "no-vault"))

    def test_canary_runs_one_exam_per_agent_and_writes_the_note(self):
        vault = self.tmp / "vault"
        vault.mkdir()
        fakes = {"codex": good_agent, "copilot": crashing_agent, "gemini": gone_agent}
        out = io.StringIO()
        with mock.patch.object(agents, "available", return_value=fakes), contextlib.redirect_stdout(out):
            rc = cli.main(["--scores", str(self.scores_file), "--vault", str(vault), "canary"])
        self.assertEqual(rc, 0)
        s = core.load_scores(self.scores_file)
        self.assertEqual(sorted(s["agents"]), ["codex", "copilot"])
        self.assertEqual(sum(len(v) for v in s["agents"]["codex"].values()), 1)
        self.assertTrue((vault / "Dev" / "Agent-Scores.md").exists())
        self.assertIn("SKIPPED", out.getvalue())

    def test_run_filters_by_category(self):
        out = io.StringIO()
        with mock.patch.object(agents, "available", return_value={"codex": good_agent}), contextlib.redirect_stdout(out):
            cli.main(["--scores", str(self.scores_file), "--vault", str(self.tmp / "nope"), "run", "--category", "study"])
        s = core.load_scores(self.scores_file)
        self.assertEqual(list(s["agents"]["codex"]), ["study"])
        self.assertEqual(len(s["agents"]["codex"]["study"]), len(core.load_bank("study")))


if __name__ == "__main__":
    unittest.main()
