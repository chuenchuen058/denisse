"""Tests for orchestra/runner.py and hooks/guard.py with a fake lane (no model calls).

    .venv\\Scripts\\python.exe orchestra\\test_runner.py
Each test builds a throwaway git repo, so the real queue is never touched.
"""
# checks: slow  (~29s: builds real git repos and runs fake lanes)
import datetime as dt
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "hooks"))
sys.path.append(str(HERE.parent))  # testing_git; appended so it never shadows an orchestra module
import guard  # noqa: E402
import testing_git  # noqa: E402
# Never a real window or toast from a test (orchestra/visible.py: the live window and toasts).
os.environ["ORCHESTRA_LIVE_WINDOW"] = "false"
os.environ["ORCHESTRA_TOASTS"] = "false"

# Fake lane, steered by env vars: FAKE_RESULT (PASS/PARTIAL/FAIL/NONE), FAKE_HUMAN,
# FAKE_SLEEP, FAKE_COMMIT, FAKE_STRAY (a path to write outside the worktree), FAKE_JUNK,
# FAKE_LIMIT (print this and exit with no report), FAKE_HOOKLOG (pretend the guard hook fired),
# FAKE_USAGE (JSON merged into the final result event: usage, modelUsage, session_id),
# FAKE_PROMPT_OUT (write the prompt received on stdin to this path).
FAKE = textwrap.dedent("""
    import os, sys, time, subprocess, pathlib, json
    _prompt = sys.stdin.read()
    if os.environ.get("FAKE_PROMPT_OUT"):
        pathlib.Path(os.environ["FAKE_PROMPT_OUT"]).write_text(_prompt, encoding="utf-8")
    if os.environ.get("FAKE_JUNK_CHECK") and pathlib.Path("junk.txt").exists():
        pathlib.Path(os.environ["FAKE_JUNK_CHECK"]).write_text("dirty worktree reused")
    if os.environ.get("FAKE_JUNK"):
        pathlib.Path("junk.txt").write_text("leftover")
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
    if os.environ.get("FAKE_HOOKLOG"):
        pathlib.Path(".review", "hook.log").write_text("fake hook")
    if os.environ.get("FAKE_LIMIT"):
        print(os.environ["FAKE_LIMIT"])
        sys.exit(1)
    if os.environ.get("FAKE_COMMIT"):
        pathlib.Path("done.txt").write_text("x")
        subprocess.run(["git", "add", "done.txt"], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fake"], check=True)
    if os.environ.get("FAKE_STRAY"):
        pathlib.Path(os.environ["FAKE_STRAY"]).write_text("oops")
    r = os.environ.get("FAKE_RESULT", "PASS")
    if r != "NONE":
        pathlib.Path(os.environ["ORCHESTRA_REPORT"]).write_text(
            f"RESULT: {r}\\nGATE: fake -> ok\\nHUMAN: {os.environ.get('FAKE_HUMAN', 'none')}\\n"
            f"PREMISE: {os.environ.get('FAKE_PREMISE', 'right')}\\n")
    print(json.dumps({"type": "result", "total_cost_usd": float(os.environ.get("FAKE_COST", "0.25")),
                      "permission_denials": json.loads(os.environ.get("FAKE_DENIED", "[]")),
                      **json.loads(os.environ.get("FAKE_USAGE", "{}"))}))
""")

GATE_OK = f'"{sys.executable}" -c "pass"'
GATE_FAIL = f'"{sys.executable}" -c "raise SystemExit(1)"'
TASK = """# {title}
Queue: {queue}
Priority: {priority}
Weight: light
Kind: {kind}
Lane: fake
Written: {written}

Do the thing.{extra}

## Gate
It works.

```
{gate}
```
"""
FAKE_ENV = ("FAKE_RESULT", "FAKE_HUMAN", "FAKE_SLEEP", "FAKE_COMMIT", "FAKE_STRAY", "FAKE_JUNK",
            "FAKE_JUNK_CHECK", "FAKE_PREMISE", "FAKE_DENIED", "FAKE_COST", "FAKE_LIMIT", "FAKE_HOOKLOG", "FAKE_USAGE",
            "FAKE_PROMPT_OUT", "ORCHESTRA_LANE_FALLBACK", "ORCHESTRA_CONSULT_FILE")


class RunnerTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)  # before any git call: a leaked GIT_DIR would hit the real repo
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
        (self.repo / ".venv").mkdir()
        (self.repo / ".venv" / "keep.txt").write_text("real venv")
        (self.repo / ".review").mkdir()
        (self.repo / ".review" / "lanes.json").write_text(json.dumps({"fake": [sys.executable, "-c", FAKE]}))
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_BACKOFF_MIN"] = "0"
        os.environ["ORCHESTRA_DAILY_SPEND_CAP_USD"] = "5"  # the spend test is written for a $5 cap
        os.environ["ORCHESTRA_TIMEOUT_S"] = json.dumps({"light": 3, "heavy": 6})
        os.environ["ORCHESTRA_POWER"] = "ac"  # never depend on whether the laptop is plugged in
        for k in FAKE_ENV:
            os.environ.pop(k, None)
        import runner
        self.R = importlib.reload(runner)

    def tearDown(self):
        for wt in (self.R.WORKTREES.glob("*") if self.R.WORKTREES.exists() else []):
            self.R.unlink_link(wt / ".venv")
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, slug, title="t", queue="auto", priority="normal", kind="work", written=None,
            gate=GATE_OK, extra="", lane="fake", headers=""):
        p = self.R.QUEUE / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        written = written or dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
        p.write_text(TASK.format(title=title, queue=queue, priority=priority, kind=kind,
                                 written=written, gate=gate, extra=extra)
                     .replace("Lane: fake\n", f"Lane: {lane}\n{headers}"))
        return p

    def state(self):
        return json.loads(self.R.STATE_FILE.read_text())

    def why(self, slug):
        t = next(t for t in self.R.load_queue() if t.slug == slug)
        return self.R.why_not_runnable(t, self.R.load_state())

    # --- local-team tasks: re-check the premise before dispatch

    def _sidecar(self, slug, evidence):
        import hashlib
        files = {}
        for rel in evidence:
            data = (self.repo / rel).read_bytes()
            files[rel] = {"size": len(data), "sha1": hashlib.sha1(data).hexdigest()}
        side = self.R.REVIEW / "local_team" / "packets" / f"{slug}.json"
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps({"evidence_files": files}), encoding="utf-8")
        return side

    def test_local_team_task_with_unchanged_evidence_runs(self):
        log = self.R.REVIEW / "evidence.log"
        log.write_text("PARKED widget: timeout\n", encoding="utf-8")
        self.add("lt")
        self._sidecar("lt", [".review/evidence.log"])
        self.assertIsNone(self.why("lt"))
        with log.open("a", encoding="utf-8") as f:  # appending to a log isn't a moved premise
            f.write("START other\n")
        self.assertIsNone(self.why("lt"))

    def test_local_team_task_whose_evidence_changed_is_refused(self):
        log = self.R.REVIEW / "evidence.log"
        log.write_text("PARKED widget: timeout\n", encoding="utf-8")
        self.add("lt")
        side = self._sidecar("lt", [".review/evidence.log"])
        log.write_text("PARKED widget: fixed by hand\n", encoding="utf-8")
        self.assertIn("premise moved, re-check", self.why("lt"))
        self.assertIsNone(self.R.pick(self.R.load_queue(), self.R.load_state()))
        log.unlink()
        self.assertIn("is gone", self.why("lt"))
        side.unlink()  # a human re-checked it and released it
        self.assertIsNone(self.why("lt"))

    def test_task_without_sidecar_is_not_premise_checked(self):
        self.add("plain")
        side = self.R.REVIEW / "local_team" / "packets" / "other.json"
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps({"evidence_files": {"nope.txt": {"size": 3, "sha1": "x"}}}))
        self.assertIsNone(self.why("plain"))

    # --- PASS is checked, not trusted

    def test_pass_with_green_runner_gate_merges_and_archives(self):
        self.add("a")
        os.environ["FAKE_COMMIT"] = "1"
        self.assertEqual(self.R.tick(), "PASS")
        day = next(self.R.ARCHIVE.iterdir())
        self.assertTrue((day / "a.md").exists() and (day / "a.report.md").exists())
        self.assertTrue((self.repo / "done.txt").exists(), "branch should be merged")
        self.assertIn("pass", self.state()["tasks"]["a"]["history"][-1]["runner_gate"])
        self.assertTrue((self.repo / ".venv" / "keep.txt").exists(), "removing the worktree must not touch the real venv")

    def test_pass_with_red_runner_gate_is_rejected(self):
        self.add("b", gate=GATE_FAIL)
        os.environ["FAKE_COMMIT"] = "1"
        self.assertEqual(self.R.tick(), "PASS-REJECTED")
        self.assertFalse((self.repo / "done.txt").exists())
        self.assertIn("runner's own gate check: fail", (self.R.PARKED / "b.why.txt").read_text())

    def test_pass_without_command_block_is_not_merged(self):
        p = self.add("c")
        p.write_text(p.read_text().split("```")[0] + "Looks right.\n")
        self.assertEqual(self.R.tick(), "PASS-REJECTED")
        self.assertIn("none", (self.R.PARKED / "c.why.txt").read_text())

    def test_unrelated_dirty_file_does_not_block_merge(self):
        (self.repo / "notes.txt").write_text("v1")
        subprocess.run(["git", "add", "notes.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "notes"], cwd=self.repo, check=True)
        (self.repo / "notes.txt").write_text("my own edit")  # Chuen's uncommitted, unrelated work
        self.add("u")
        os.environ["FAKE_COMMIT"] = "1"
        self.assertEqual(self.R.tick(), "PASS")
        self.assertTrue((self.repo / "done.txt").exists(), "merged despite the unrelated edit")
        self.assertEqual((self.repo / "notes.txt").read_text(), "my own edit")

    def test_dirty_file_the_branch_touches_blocks_merge(self):
        (self.repo / "done.txt").write_text("v1")
        subprocess.run(["git", "add", "done.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "done"], cwd=self.repo, check=True)
        (self.repo / "done.txt").write_text("my own edit")
        self.add("v")
        os.environ["FAKE_COMMIT"] = "1"
        self.assertEqual(self.R.tick(), "PASS")
        self.assertIn("done.txt", self.state()["tasks"]["v"]["merge"])
        self.assertEqual((self.repo / "done.txt").read_text(), "my own edit")

    def test_main_checkout_write_blocks_merge(self):
        self.add("w")
        os.environ["FAKE_STRAY"] = str(self.repo / "stray.txt")
        self.assertEqual(self.R.tick(), "PASS-REJECTED")
        self.assertIn("stray.txt", (self.R.PARKED / "w.why.txt").read_text())

    def test_same_content_does_not_run_twice(self):
        p = self.add("d")
        text = p.read_text()
        self.R.tick()
        (self.R.QUEUE / "d2.md").write_text(text)
        self.assertIn("already PASSed", self.why("d2"))

    # --- no blind retries

    def test_partial_and_fail_park_at_once(self):
        self.add("e")
        os.environ["FAKE_RESULT"] = "FAIL"
        self.assertEqual(self.R.tick(), "FAIL")
        self.assertIn("blind re-run", (self.R.PARKED / "e.why.txt").read_text())
        self.assertEqual(self.R.tick(), "idle")

    def test_human_line_parks(self):
        self.add("h")
        os.environ["FAKE_RESULT"] = "PARTIAL"
        os.environ["FAKE_HUMAN"] = "sign in to Outlook"
        self.R.tick()
        self.assertIn("waiting on Chuen", (self.R.PARKED / "h.why.txt").read_text())
        self.assertIn("sign in to Outlook", (self.R.REVIEW / "STATE_NOW.md").read_text())

    def test_timeout_retries_once_in_a_fresh_worktree_then_parks(self):
        self.add("t")
        os.environ["FAKE_SLEEP"] = "30"
        os.environ["FAKE_JUNK"] = "1"
        self.assertEqual(self.R.tick(), "TIMEOUT")
        flag = self.tmp / "reused.txt"
        os.environ["FAKE_JUNK_CHECK"] = str(flag)
        self.assertEqual(self.R.tick(), "TIMEOUT")
        self.assertFalse(flag.exists(), "attempt 2 must not start on attempt 1's leftovers")
        self.assertTrue((self.R.PARKED / "t.md").exists())
        h = self.state()["tasks"]["t"]["history"][-1]
        self.assertLess(h["seconds"], 15)
        self.assertIn("estimated", h["spent"])

    def test_no_report_is_infra(self):
        self.add("n")
        os.environ["FAKE_RESULT"] = "NONE"
        self.assertEqual(self.R.tick(), "NO_REPORT")
        self.assertFalse((self.R.PARKED / "n.md").exists(), "one retry first")

    def test_editing_a_parked_task_gives_it_a_fresh_start(self):
        p = self.add("f")
        os.environ["FAKE_RESULT"] = "FAIL"
        self.R.tick()
        parked = self.R.PARKED / "f.md"
        p.write_text(parked.read_text().replace("Do the thing.", "Do the thing, properly."))
        parked.unlink()
        self.assertIsNone(self.why("f"))

    # --- picking and limits

    def test_urgent_then_work_then_maintenance(self):
        self.add("1-maint", kind="maintenance", extra=" 1")
        self.add("2-work", extra=" 2")
        self.add("3-urgent", priority="urgent", extra=" 3")
        st = self.R.load_state()
        self.assertEqual(self.R.pick(self.R.load_queue(), st).slug, "3-urgent")
        (self.R.QUEUE / "3-urgent.md").unlink()
        self.assertEqual(self.R.pick(self.R.load_queue(), st).slug, "2-work")

    def test_maintenance_cap(self):
        self.add("m", kind="maintenance")
        st = self.R.load_state()
        u = self.R.today_usage(st)
        u.update(work_s=3600, maint_s=1800)
        self.assertIsNone(self.R.pick(self.R.load_queue(), st))
        u.update(work_s=36000, maint_s=1800)
        self.assertEqual(self.R.pick(self.R.load_queue(), st).slug, "m")

    def test_spend_is_recorded_and_capped(self):
        self.add("s1", extra=" 1")
        os.environ["FAKE_COST"] = "4.0"
        self.R.tick()
        self.assertAlmostEqual(self.R.today_usage(self.R.load_state())["cost_usd"], 4.0)
        self.add("s2", extra=" 2")
        os.environ["FAKE_COST"] = "1.5"
        self.R.tick()
        self.add("s3", extra=" 3")
        self.assertIn("spend cap", self.why("s3"))

    def test_timeout_comes_from_measured_runs(self):
        st = self.R.load_state()
        self.assertEqual(self.R.timeout_for("light", st), 3)
        st["durations"]["light"] = [200] * 20
        self.assertEqual(self.R.timeout_for("light", st), 600)

    def test_bad_header_stale_draft_placeholder(self):
        self.add("x1", priority="high", extra=" 1")
        self.add("x2", written=(dt.datetime.now() - dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M"), extra=" 2")
        self.add("x3", queue="draft", extra=" 3")
        p = self.add("x4", extra=" 4")
        p.write_text(p.read_text().replace("It works.", "<Exactly how>").replace("## Gate\n", "## Gate\n<x>\n"))
        self.assertIn("bad header", self.why("x1"))
        self.assertIn("stale", self.why("x2"))
        self.assertEqual(self.why("x3"), "Queue is not auto")
        self.assertIn("placeholder", self.why("x4"))
        self.assertEqual(self.R.tick(), "idle")

    def test_lock_is_exclusive(self):
        self.add("k")
        self.R.LOCK_FILE.write_text("{}")
        self.assertEqual(self.R.tick(), "busy")

    def test_premise_and_denials_reach_state_now(self):
        self.add("p")
        os.environ["FAKE_RESULT"] = "PARTIAL"
        os.environ["FAKE_PREMISE"] = "wrong: it was already fixed"
        os.environ["FAKE_DENIED"] = json.dumps([{"tool_name": "Bash", "tool_input": {"command": "npm test"}}])
        self.R.tick()
        h = self.state()["tasks"]["p"]["history"][-1]
        self.assertEqual(h["denied"], ["Bash(npm)"])
        sn = (self.R.REVIEW / "STATE_NOW.md").read_text()
        self.assertIn("refused Bash(npm)", sn)
        self.assertIn("premise was wrong", sn)

    # --- the study tutor consulted before the lane (orchestra/consult.py, advisory)

    def consult_with(self, mode="ok", notes="crash"):
        """A fake tutor command in a copy of consult.json (a tutor installed in the repo, the GPU
        free, short limits), and the lane's prompt captured."""
        import test_consult
        cfg = json.loads((HERE / "consult.json").read_text(encoding="utf-8"))
        cfg["command"] = [sys.executable, "-c", test_consult.FAKE_DON, "{question}"]
        cfg["notes_command"] = [sys.executable, "-c", test_consult.FAKE_DON, "{question}", "--notes-only"]
        f = self.tmp / "consult.json"
        f.write_text(json.dumps(cfg), encoding="utf-8")
        os.environ["ORCHESTRA_CONSULT_FILE"] = str(f)
        self.addCleanup(os.environ.pop, "ORCHESTRA_CONSULT_FILE", None)
        (self.repo / "tutor").mkdir(exist_ok=True)
        (self.repo / "tutor" / "tutor.json").write_text(json.dumps({"name": "Don"}), encoding="utf-8")
        subprocess.run(["git", "add", "tutor"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "tutor"], cwd=self.repo, check=True, capture_output=True)
        os.environ["ORCHESTRA_GPU_LEASE"] = str(self.tmp / "gpu.lease")
        self.addCleanup(os.environ.pop, "ORCHESTRA_GPU_LEASE", None)
        busy = mock.patch.object(self.R.consult, "gpu_busy", return_value=None)
        busy.start()
        self.addCleanup(busy.stop)
        self.R.C.update(consult_timeout_s=5, consult_notes_timeout_s=5, consult_busy_wait_s=0)
        os.environ["FAKE_DON"] = mode
        os.environ["FAKE_NOTES"] = notes
        self.addCleanup(os.environ.pop, "FAKE_DON", None)
        self.addCleanup(os.environ.pop, "FAKE_NOTES", None)
        prompt = self.tmp / "prompt.txt"
        os.environ["FAKE_PROMPT_OUT"] = str(prompt)
        return prompt

    def test_consulted_task_gets_the_tutors_note_and_a_prompt_line(self):
        prompt = self.consult_with()
        self.add("study", headers="Consult: don\n", extra=" Make tutor/chat.py quiz shorter.")
        self.assertEqual(self.R.tick(), "PASS")
        text = prompt.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(self.R.build_prompt(".review/task.md", ".review/report.md")),
                        "the normal prompt first, untouched")
        self.assertIn("`.review/don.md`", text)
        self.assertIn("never an override", text)
        log = self.R.LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("CONSULT study: ", log)
        self.assertIn("path=model -> high confidence by local", log)
        self.assertIn(".review/don.md written", log)

    def test_tutor_that_cannot_answer_never_stops_the_task(self):
        prompt = self.consult_with("crash")
        self.add("study2", extra=" Edit study_brief.py to read the new sidecar.")
        self.assertEqual(self.R.tick(), "PASS")
        self.assertEqual(prompt.read_text(encoding="utf-8"), self.R.build_prompt(".review/task.md", ".review/report.md"))
        log = self.R.LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("CONSULT study2: ", log)
        self.assertIn("could not answer", log)

    def test_plain_task_is_consulted_under_the_all_rule_and_not_when_narrowed_or_named(self):
        prompt = self.consult_with()
        # Shipped (12 Nov 2026, docs/orchestra_vs_thomas.md): consult_applies all, capped by
        # consult_narrow_to "named", so a plain task is not put to the tutor.
        self.assertEqual((self.R.C["consult_applies"], self.R.C["consult_narrow_to"]), ("all", "named"))
        self.add("plain0", extra=" A plain task under the shipped rule.")
        self.assertEqual(self.R.tick(), "PASS")
        self.assertEqual(prompt.read_text(encoding="utf-8"), self.R.build_prompt(".review/task.md", ".review/report.md"))
        self.assertNotIn("CONSULT plain0", self.R.LOG_FILE.read_text(encoding="utf-8"))
        self.R.C["consult_narrow_to"] = ""  # the all rule itself: every task, any lane
        self.add("plain")
        self.assertEqual(self.R.tick(), "PASS")
        self.assertIn("`.review/don.md`", prompt.read_text(encoding="utf-8"), "every task, any lane")
        self.assertIn("CONSULT plain: ", self.R.LOG_FILE.read_text(encoding="utf-8"))
        self.R.C["consult_applies"] = "named"
        self.add("plain2", extra=" A different plain task.")
        self.assertEqual(self.R.tick(), "PASS")
        self.assertEqual(prompt.read_text(encoding="utf-8"), self.R.build_prompt(".review/task.md", ".review/report.md"))
        self.assertNotIn("CONSULT plain2", self.R.LOG_FILE.read_text(encoding="utf-8"))

    def test_busy_or_failed_tutor_gives_the_notes_only_opinion(self):
        prompt = self.consult_with("crash", notes="ok")
        self.R.C["consult_narrow_to"] = ""  # a plain task is consulted only under the un-narrowed all rule
        self.add("busy")
        self.assertEqual(self.R.tick(), "PASS")
        self.assertIn("confidence: medium", prompt.read_text(encoding="utf-8"))
        log = self.R.LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("CONSULT busy: ", log)
        self.assertIn("path=notes", log)

    def test_claude_lane_gets_the_guard_settings(self):
        wt = self.tmp / "wt"
        (wt / ".review").mkdir(parents=True)
        s = json.loads(self.R.hook_settings(wt).read_text())
        cmd = s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn("guard.py", cmd)


    # --- usage limits: come back later, not a failed attempt

    def lanes_with(self, *names, fallback=None):
        """Real lane names (codex, claude) backed by the fake lane; fallback=None keeps the config's."""
        lanes = {n: [sys.executable, "-c", FAKE] for n in ("fake", *names)}
        (self.R.REVIEW / "lanes.json").write_text(json.dumps(lanes))
        if fallback is not None:
            os.environ["ORCHESTRA_LANE_FALLBACK"] = json.dumps(fallback)  # setUp clears it
            self.R = importlib.reload(self.R)

    def test_codex_limit_is_not_an_attempt(self):
        self.lanes_with("codex", fallback={})
        self.add("lim", lane="codex")
        os.environ["FAKE_LIMIT"] = self.R.CODEX_LIMIT_FIXTURE
        self.assertEqual(self.R.tick(), "LIMITED")
        st = self.state()
        self.assertEqual(st["tasks"]["lim"]["attempts"], 0, "a limit must not use up the retry")
        self.assertTrue((self.R.QUEUE / "lim.md").exists(), "stays queued, not parked")
        self.assertFalse((self.R.PARKED / "lim.md").exists())
        until = dt.datetime.fromisoformat(st["lanes"]["codex"]["limited_until"])
        self.assertEqual((until.hour, until.minute), (22, 55))
        self.assertGreater(until, dt.datetime.now())
        self.assertIn("LIMITED lim lane=codex until", self.R.LOG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(self.why("lim"), "lane codex limited until 22:55")
        self.assertEqual(self.R.tick(), "idle")
        self.assertIn("**Limited lanes:** codex until", (self.R.REVIEW / "STATE_NOW.md").read_text(encoding="utf-8"))

    def test_limit_falls_back_to_claude_unless_lane_fixed(self):
        self.lanes_with("codex", "claude")  # config default: codex -> claude
        self.add("fb", lane="codex", extra=" fb")
        self.add("fixed", lane="codex", headers="Lane-Fixed: yes\n", extra=" fixed")
        st = self.R.load_state()
        st["lanes"]["codex"] = {"limited_until": (dt.datetime.now() + dt.timedelta(hours=1)).isoformat()}
        self.R.save_state(st)
        self.assertIsNone(self.why("fb"), "a fallback exists, so it may run")
        self.assertIn("lane codex limited until", self.why("fixed"))
        os.environ["FAKE_COMMIT"] = "1"
        os.environ["FAKE_HOOKLOG"] = "1"
        self.assertEqual(self.R.tick(), "PASS")
        h = self.state()["tasks"]["fb"]["history"][-1]
        self.assertEqual((h["lane"], h["fallback_from"]), ("claude", "codex"))
        self.assertIn("START fb lane=claude (fallback: lane codex limited until",
                      self.R.LOG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(self.R.tick(), "idle", "the Lane-Fixed task waits for codex")

    def test_unrelated_no_report_is_still_infra(self):
        self.lanes_with("codex")
        self.add("nr", lane="codex")
        os.environ["FAKE_RESULT"] = "NONE"
        self.assertEqual(self.R.tick(), "NO_REPORT")
        st = self.state()
        self.assertEqual(st["tasks"]["nr"]["attempts"], 1)
        self.assertNotIn("codex", st["lanes"])

    def test_limit_patterns_per_lane(self):
        log = self.tmp / "x.lane.log"
        cases = [
            ("codex", "some output\n" + self.R.CODEX_LIMIT_FIXTURE + "\n", True),
            ("claude", json.dumps({"type": "result", "is_error": True,
                                   "result": "Claude AI usage limit reached|1893456000"}) + "\n", True),
            ("claude", "5-hour limit reached - resets 3am (Europe/London)\n", True),
            ("gemini", "Error: IneligibleTierError: not eligible\n", True),
            ("gemini", "Quota exceeded for quota metric 'Requests per day'\n", True),
            # a session that only *read* about limits in a tool result is not limited
            ("claude", json.dumps({"type": "user", "message": {"content": "usage limit reached"}}) + "\n"
                       + json.dumps({"type": "result", "result": "done"}) + "\n", False),
            ("codex", "all fine, no report though\n", False),
        ]
        for lane, text, hit in cases:
            log.write_text(text, encoding="utf-8")
            self.assertEqual(bool(self.R.detect_limit(lane, log)), hit, (lane, text))

    def test_parse_limit_until(self):
        at = dt.datetime(2026, 11, 2, 20, 0)
        P = lambda s: self.R.parse_limit_until(s, at)
        self.assertEqual(P(self.R.CODEX_LIMIT_FIXTURE), dt.datetime(2026, 11, 2, 22, 55))
        self.assertEqual(P("limit reached, resets at 3am"), dt.datetime(2026, 11, 3, 3, 0))
        self.assertEqual(P("5-hour limit reached - resets 3am (Europe/London)"), dt.datetime(2026, 11, 3, 3, 0))
        self.assertEqual(P("try again at 12:30 AM"), dt.datetime(2026, 11, 3, 0, 30))
        self.assertEqual(P("try again at 21:15"), dt.datetime(2026, 11, 2, 21, 15))
        self.assertEqual(P("try again in 20 minutes"), dt.datetime(2026, 11, 2, 20, 20))
        self.assertEqual(P("or try again at Nov 4th, 2026 9:05 AM."), dt.datetime(2026, 11, 4, 9, 5))
        self.assertIsNone(P("ERROR: usage limit hit."))
        self.assertIsNone(P("IneligibleTierError"))

    def test_unparseable_limit_waits_an_hour(self):
        self.lanes_with("codex", fallback={})
        self.add("lim2", lane="codex")
        os.environ["FAKE_LIMIT"] = "ERROR: usage limit hit, come back later."
        before = dt.datetime.now()
        self.assertEqual(self.R.tick(), "LIMITED")
        until = dt.datetime.fromisoformat(self.state()["lanes"]["codex"]["limited_until"])
        self.assertAlmostEqual((until - before).total_seconds(), 3600, delta=120)

    # --- copilot lane (GitHub Copilot CLI; fake executable, no model calls)

    COPILOT_FAKE = textwrap.dedent("""
        import json, os, pathlib, sys
        args = sys.argv[1:]
        sys.stdin.read()
        prompt_arg = args[args.index("-p") + 1]
        path = prompt_arg.split("file ", 1)[1].split(" ", 1)[0].rstrip(".")
        prompt = pathlib.Path(path).read_text(encoding="utf-8")
        pathlib.Path(os.environ["FAKE_COPILOT_SEEN"]).write_text(json.dumps(
            {"args": args, "prompt_path": path, "prompt": prompt,
             "worktree": os.environ.get("ORCHESTRA_WORKTREE"), "cwd": os.getcwd()}), encoding="utf-8")
        if os.environ.get("FAKE_LIMIT"):
            print(os.environ["FAKE_LIMIT"])
            sys.exit(1)
        pathlib.Path(os.environ["ORCHESTRA_REPORT"]).write_text(
            "RESULT: PASS\\nGATE: fake -> ok\\nHUMAN: none\\nPREMISE: right\\n")
        print("Total usage est: 1 Premium request")
    """)

    def copilot_lane(self):
        """lanes.json's copilot = the shipped config argv with the CLI swapped for the fake."""
        fake = self.tmp / "fake_copilot.py"
        fake.write_text(self.COPILOT_FAKE, encoding="utf-8")
        argv = [sys.executable, str(fake), *self.R.resolve_argv(self.R.C["lane_argv"]["copilot"])[1:]]
        (self.R.REVIEW / "lanes.json").write_text(json.dumps({"fake": [sys.executable, "-c", FAKE], "copilot": argv}))
        seen = self.tmp / "seen.json"
        os.environ["FAKE_COPILOT_SEEN"] = str(seen)
        self.addCleanup(os.environ.pop, "FAKE_COPILOT_SEEN", None)
        return seen

    def test_copilot_argv_shape_and_shim_from_claude_app_appdata(self):
        roaming, local = self.tmp / "roaming", self.tmp / "local"
        roaming.mkdir()
        npm = local / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "npm"
        npm.mkdir(parents=True)
        (npm / "copilot.cmd").write_text("@echo off\n")
        for k, v in (("APPDATA", roaming), ("LOCALAPPDATA", local)):
            old = os.environ.get(k)
            os.environ[k] = str(v)
            self.addCleanup(lambda k=k, old=old: os.environ.__setitem__(k, old) if old is not None else os.environ.pop(k, None))
        # a real copilot under this machine's home folder (~/AppData/Roaming/npm) must not stand in
        # for the shim the test planted: point ~ at the temp folder too
        from unittest import mock
        hide = mock.patch.dict(os.environ, {"USERPROFILE": str(self.tmp), "HOME": str(self.tmp)})
        hide.start()
        self.addCleanup(hide.stop)
        argv = self.R.lanes()["copilot"]
        self.assertEqual(Path(argv[0]), npm / "copilot.cmd", "the Claude-app npm shim is found off PATH")
        self.assertIn("--allow-all-tools", argv)
        for banned in ("--allow-all-urls", "--allow-all-paths", "--allow-all", "--yolo"):
            self.assertNotIn(banned, argv)
        self.assertEqual(argv.count("--add-dir"), 1)
        venv = (self.repo / ".venv").resolve()
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(venv))
        self.assertIn("--disable-builtin-mcps", argv)
        self.assertIn("--no-ask-user", argv)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--deny-tool"],
                         ["shell(git push)", "shell(gh)"])
        defaults = json.loads(self.R.CONFIG_DEFAULTS.read_text(encoding="utf-8"))
        self.assertEqual(self.R.C["lane_argv"]["copilot"], defaults["lane_argv"]["copilot"])
        # Also build from a linked checkout: grant the real target, not the junction
        # inside the worktree, and never its parent (the whole main checkout).
        wt = self.R.WORKTREES / "copilot linked tree with spaces"
        wt.mkdir(parents=True)
        self.R.link_venv(wt)
        self.assertTrue(self.R.is_link(wt / ".venv"))
        with mock.patch.object(self.R, "REPO", wt):
            linked_argv = self.R.lanes()["copilot"]
        self.assertEqual(linked_argv, argv)
        p = argv[argv.index("-p") + 1]
        self.assertIn(self.R.PROMPT_FILE_PLACEHOLDER, p)
        self.assertLess(len(p), 300, "only a pointer to the prompt file goes on the command line")
        self.assertIn("copilot", self.R.C["unpriced_lanes"])
        self.assertEqual(self.R.C["lane_slots"].get("copilot"), 1)
        self.assertEqual(self.R.C["lane_fallback"].get("copilot"), "claude")
        # the exams and the hub share this resolver: same folder, same CLI
        sys.path.insert(0, str(HERE.parent))
        from orchestra.exams import agents
        (npm / "node_modules" / "@github" / "copilot").mkdir(parents=True)
        (npm / "node_modules" / "@github" / "copilot" / "package.json").write_text(
            json.dumps({"bin": {"copilot": "npm-loader.js"}}))
        self.assertEqual(Path(agents._npm_entry("@github/copilot", "copilot")),
                         npm / "node_modules" / "@github" / "copilot" / "npm-loader.js")

    def test_npm_dirs_skip_unset_variables(self):
        d = self.tmp / "npmdir"
        d.mkdir()
        os.environ.pop("ORCHESTRA_TEST_UNSET_VAR", None)
        self.assertEqual(self.R.npm_dirs(["%ORCHESTRA_TEST_UNSET_VAR%/npm", str(d)]), [d])
        self.assertEqual(self.R.npm_dirs([str(self.tmp / "np*dir")]), [d])

    def test_copilot_prompt_goes_in_a_file_and_runs_in_the_worktree(self):
        seen = self.copilot_lane()
        self.add("cp", lane="copilot")
        self.assertEqual(self.R.tick(), "PASS")
        got = json.loads(seen.read_text(encoding="utf-8"))
        self.assertEqual(got["prompt"], self.R.build_prompt(".review/task.md", ".review/report.md"))
        wt = Path(got["worktree"])
        self.assertEqual(Path(got["prompt_path"]), wt / ".review" / "prompt.md")
        self.assertEqual(Path(got["cwd"]).resolve(), wt.resolve())
        self.assertEqual(got["args"].count("--add-dir"), 1)
        self.assertEqual(got["args"][got["args"].index("--add-dir") + 1],
                         str((self.repo / ".venv").resolve()))
        for banned in ("--allow-all-paths", "--allow-all-urls", "--allow-all", "--yolo"):
            self.assertNotIn(banned, got["args"])
        self.assertFalse(any(self.R.PROMPT_FILE_PLACEHOLDER in a for a in got["args"]))
        self.assertLess(max(len(a) for a in got["args"]), 400, "no long prompt on the command line")
        h = self.state()["tasks"]["cp"]["history"][-1]
        self.assertEqual(h["lane"], "copilot")
        u = self.R.today_usage(self.R.load_state())
        self.assertEqual((u["cost_usd"], u["estimated_runs"]), (0, 0), "copilot is unpriced: no estimate")

    def test_copilot_limit_messages(self):
        log = self.tmp / "c.lane.log"
        hits = ["Error: You've reached your monthly limit of premium requests. Try again at 9:00 AM.",
                "You have exhausted your premium requests quota for this month.",
                "error: out of AI credits for this billing period",
                "Request failed: 429 Too Many Requests"]
        misses = ["Total usage est: 1 Premium request", "Session limits: 0.5/1 AI credits used.",
                  "all fine, no report though"]
        for text in hits + misses:
            log.write_text(text + "\n", encoding="utf-8")
            self.assertEqual(bool(self.R.detect_limit("copilot", log)), text in hits, text)

    def test_copilot_limit_waits_and_falls_back_to_claude(self):
        seen = self.copilot_lane()
        os.environ["ORCHESTRA_LANE_FALLBACK"] = json.dumps({})
        self.R = importlib.reload(self.R)
        self.add("cl", lane="copilot")
        os.environ["FAKE_LIMIT"] = "Error: You've reached your monthly limit of premium requests."
        self.assertEqual(self.R.tick(), "LIMITED")
        self.assertTrue(seen.exists())
        st = self.state()
        self.assertEqual(st["tasks"]["cl"]["attempts"], 0, "a limit is not an attempt")
        self.assertIn("copilot", st["lanes"])
        self.assertTrue((self.R.QUEUE / "cl.md").exists())
        # with the shipped fallback (copilot -> claude) the waiting task moves to claude
        os.environ.pop("ORCHESTRA_LANE_FALLBACK")
        os.environ.pop("FAKE_LIMIT")
        self.R = importlib.reload(self.R)
        self.lanes_with("copilot", "claude")
        self.add("cf", lane="copilot", extra=" cf")
        self.assertEqual(self.R.fallback_lane(next(t for t in self.R.load_queue() if t.slug == "cf"),
                                              self.R.load_state()), "claude")
        os.environ["FAKE_COMMIT"] = "1"
        os.environ["FAKE_HOOKLOG"] = "1"
        res = self.R.tick()
        self.assertEqual(res, "PASS")
        done = {s: v["history"][-1] for s, v in self.state()["tasks"].items() if v.get("history")}
        self.assertTrue(any(h.get("fallback_from") == "copilot" and h["lane"] == "claude" for h in done.values()))

    def test_unpriced_lanes_are_never_estimated_or_capped(self):
        st = self.R.load_state()
        self.R.today_usage(st)["cost_usd"] = 99.0  # well over the $5 cap
        for lane in ("copilot", "gemini", "codex"):
            self.add(f"u{lane}", lane=lane, extra=f" {lane}")
            t = next(t for t in self.R.load_queue() if t.slug == f"u{lane}")
            self.assertEqual(self.R.charge(st, t, 600, None), "unknown", lane)
            self.R.save_state(st)
            self.assertNotIn("spend cap", self.why(f"u{lane}") or "", lane)
        self.assertEqual(self.R.today_usage(st)["cost_usd"], 99.0)
        self.add("upriced", extra=" priced")
        t = next(t for t in self.R.load_queue() if t.slug == "upriced")
        self.assertIn("estimated", self.R.charge(st, t, 600, None))

    # --- expired sign-in (3 Nov 2026: Claude's OAuth expired at 18:06; 9 tasks parked as NO_REPORT)

    AUTH_TEXT = "Failed to authenticate: OAuth session expired and could not be refreshed"

    def test_auth_patterns_come_from_config(self):
        log = self.tmp / "a.lane.log"
        log.write_text("starting\n" + self.AUTH_TEXT + "\n", encoding="utf-8")
        self.assertTrue(self.R.detect_auth("claude", log))
        self.assertIsNone(self.R.detect_limit("claude", log), "a sign-in failure is not a usage limit")
        log.write_text(json.dumps({"type": "result", "is_error": True, "result": self.AUTH_TEXT}) + "\n",
                       encoding="utf-8")
        self.assertTrue(self.R.detect_auth("claude", log), "stream-json result line")
        log.write_text("all fine, no report though\n", encoding="utf-8")
        self.assertIsNone(self.R.detect_auth("claude", log))
        old = self.R.C.get("auth_patterns")
        self.addCleanup(self.R.C.__setitem__, "auth_patterns", old)
        self.R.C["auth_patterns"] = {"claude": ["custom sign-in wording"]}
        log.write_text("Custom sign-in wording here\n", encoding="utf-8")
        self.assertTrue(self.R.detect_auth("claude", log), "patterns are data, not code")

    def test_expired_sign_in_is_not_an_attempt(self):
        self.lanes_with("codex", fallback={})
        self.add("auth1", lane="codex")
        os.environ["FAKE_LIMIT"] = "Error: not logged in. Please log in with `codex login`."
        old = self.R.C.get("auth_retry_min")
        self.addCleanup(self.R.C.__setitem__, "auth_retry_min", old)
        self.R.C["auth_retry_min"] = 7
        before = dt.datetime.now()
        self.assertEqual(self.R.tick(), "LIMITED")
        st = self.state()
        self.assertEqual(st["tasks"]["auth1"]["attempts"], 0, "the attempt isn't counted")
        until = dt.datetime.fromisoformat(st["lanes"]["codex"]["limited_until"])
        self.assertAlmostEqual((until - before).total_seconds(), 7 * 60, delta=120)
        self.assertIn("sign", st["lanes"]["codex"]["message"].lower())

    # --- reconcile after a killed tick (2 Nov 2026: on battery the ticks died, their lanes didn't)

    def dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def live_child(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def write_lock(self, **info):
        info.setdefault("since", dt.datetime.now().isoformat())
        self.R.LOCK_FILE.write_text(json.dumps(info))

    def orphan_worktree(self, slug, report=None, commit=False):
        """What a killed tick leaves: attempt counted, worktree made, lane maybe finished."""
        task = next(t for t in self.R.load_queue() if t.slug == slug)
        st = self.R.load_state()
        ts = self.R.task_state(st, task)
        ts["attempts"] += 1
        ts["last_start"] = dt.datetime.now().isoformat()
        self.R.save_state(st)
        wt, _ = self.R.fresh_worktree(task)
        if commit:
            (wt / "done.txt").write_text("x")
            subprocess.run(["git", "add", "done.txt"], cwd=wt, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-qm", "orphan's work"], cwd=wt, check=True, capture_output=True)
        if report:
            (wt / ".review").mkdir(exist_ok=True)
            (wt / ".review" / "report.md").write_text(report)
        return wt

    def test_creation_time_identifies_a_process(self):
        me = self.R.proc_created(os.getpid())
        self.assertTrue(me, "own creation time must be readable")
        self.assertTrue(self.R._alive(os.getpid(), me))
        self.assertFalse(self.R._alive(os.getpid(), me + 1))
        self.assertIsNone(self.R.proc_created(self.dead_pid()))

    def test_dead_owner_lock_is_reclaimed_at_once(self):
        self.add("dl")
        self.write_lock(pid=self.dead_pid(), created=12345)  # seconds old, far under the age rule
        os.environ["FAKE_COMMIT"] = "1"
        self.assertEqual(self.R.tick(), "PASS")
        self.assertIn("RECONCILE dead owner", self.R.LOG_FILE.read_text(encoding="utf-8"))

    def test_reused_pid_is_treated_as_dead(self):
        self.add("ru")
        child = self.live_child()  # alive, same pid, but not the process the lock was written by
        real = self.R.proc_created(child.pid)
        self.R.proc_created = lambda pid, _real=self.R.proc_created: (real + 999 if pid == child.pid else _real(pid))
        self.write_lock(pid=child.pid, created=real)
        self.assertFalse(self.R._alive(child.pid, real))
        self.assertEqual(self.R.tick(), "PASS")
        self.assertIn(f"RECONCILE dead owner {child.pid}", self.R.LOG_FILE.read_text(encoding="utf-8"))

    def test_unparseable_lock_falls_back_to_age_and_is_kept(self):
        self.add("up")
        self.R.LOCK_FILE.write_text("not json")
        self.assertEqual(self.R.tick(), "busy")
        self.assertTrue(self.R.LOCK_FILE.exists(), "never delete a lock just because it didn't parse")

    def test_live_orphan_blocks_redispatch(self):
        self.add("or")
        self.orphan_worktree("or")
        child = self.live_child()
        self.write_lock(pid=self.dead_pid(), created=1, task="or", lane="fake",
                        lane_pid=child.pid, lane_created=self.R.proc_created(child.pid))
        self.assertEqual(self.R.tick(), "busy")
        self.assertEqual(self.state()["tasks"]["or"]["attempts"], 1, "not dispatched again")
        self.assertTrue(self.R.LOCK_FILE.exists(), "the slot stays occupied")
        self.assertTrue((self.R.QUEUE / "or.md").exists())
        self.assertIn(f"RECONCILE orphan lane {child.pid} still running or",
                      self.R.LOG_FILE.read_text(encoding="utf-8"))
        # in parallel mode another slot must not pick it up either
        self.assertEqual(self.R.reconcile(self.R.load_state()), {"or": "orphan running"})

    def test_orphan_report_is_collected_and_merged(self):
        self.add("rp")
        self.orphan_worktree("rp", report="RESULT: PASS\nGATE: ok\nHUMAN: none\nPREMISE: right\n", commit=True)
        self.write_lock(pid=self.dead_pid(), created=1, task="rp", lane="fake",
                        lane_pid=self.dead_pid(), lane_created=1, before=[])
        self.assertEqual(self.R.tick(), "idle")
        self.assertTrue((self.repo / "done.txt").exists(), "the orphan's branch is merged")
        ts = self.state()["tasks"]["rp"]
        self.assertEqual(ts["attempts"], 1)
        self.assertEqual(ts["merge"], "merged")
        h = ts["history"][-1]
        self.assertEqual(h["result"], "PASS")
        self.assertTrue(h["recovered"])
        self.assertIn("pass", h["runner_gate"])
        day = next(self.R.ARCHIVE.iterdir())
        self.assertTrue((day / "rp.md").exists() and (day / "rp.report.md").exists())
        self.assertFalse(self.R.LOCK_FILE.exists())

    def test_orphan_without_report_counts_one_attempt(self):
        self.add("nr2")
        wt = self.orphan_worktree("nr2")
        self.write_lock(pid=self.dead_pid(), created=1, task="nr2", lane="fake",
                        lane_pid=self.dead_pid(), lane_created=1, before=[])
        self.assertEqual(self.R.reconcile(self.R.load_state()), {"nr2": "NO_REPORT"})
        ts = self.state()["tasks"]["nr2"]
        self.assertEqual((ts["attempts"], ts["last_result"]), (1, "NO_REPORT"))
        self.assertTrue(ts["history"][-1]["recovered"])
        self.assertTrue(wt.exists(), "worktree kept")
        self.assertTrue((self.R.QUEUE / "nr2.md").exists(), "one retry left")
        self.assertFalse(self.R.LOCK_FILE.exists(), "slot freed")
        os.environ["FAKE_RESULT"] = "NONE"  # the retry fails the same way: parked, no third run
        self.assertEqual(self.R.tick(), "NO_REPORT")
        self.assertEqual(self.state()["tasks"]["nr2"]["attempts"], 2)
        self.assertTrue((self.R.PARKED / "nr2.md").exists())

    def test_parallel_state_saves_keep_lane_limits(self):
        base = self.R.load_state()
        mine = json.loads(json.dumps(base))
        mine["lanes"]["codex"] = {"limited_until": "2026-11-02T22:55:00"}
        out = self.R._merge_into(self.R.load_state(), mine, base)
        self.assertEqual(out["lanes"]["codex"]["limited_until"], "2026-11-02T22:55:00")

    # --- provenance: which prompt, commit, model and tokens produced each result

    USAGE = {"session_id": "sess-123",
             "usage": {"input_tokens": 32, "output_tokens": 500, "cache_read_input_tokens": 9000,
                       "cache_creation_input_tokens": 700},
             "modelUsage": {"claude-haiku-5-5": {"outputTokens": 10, "costUSD": 0.01},
                            "claude-opus-5-5": {"outputTokens": 490, "costUSD": 0.24}}}

    def test_provenance_is_recorded_from_the_lanes_json(self):
        self.add("pv")
        os.environ["FAKE_USAGE"] = json.dumps(self.USAGE)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                              text=True, check=True).stdout.strip()
        self.assertEqual(self.R.tick(), "PASS")
        h = self.state()["tasks"]["pv"]["history"][-1]
        sent = self.R.build_prompt(".review/task.md", ".review/report.md").replace("\r\n", "\n")
        self.assertEqual(h["prompt_sha"], hashlib.sha256(sent.encode("utf-8")).hexdigest()[:12])
        tmpl = (self.repo / "orchestra" / "review_prompt.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(h["prompt_file_sha"], hashlib.sha256(tmpl.encode("utf-8")).hexdigest()[:12])
        self.assertNotEqual(h["prompt_sha"], h["prompt_file_sha"], "rendered text, not the template")
        self.assertEqual(h["base_commit"], base)
        self.assertEqual(h["lane"], "fake")
        self.assertEqual(h["model"], "claude-opus-5-5", "the model that did most of the work")
        self.assertEqual(h["tokens"], {"in": 32, "out": 500, "cache_read": 9000, "cache_write": 700})
        self.assertEqual(h["session_id"], "sess-123")
        log = self.R.LOG_FILE.read_text(encoding="utf-8")
        self.assertIn(f"prompt {h['prompt_sha']} tokens in 32 out 500 cache-read 9000 cache-write 700", log)

    def test_missing_usage_gives_nulls_not_zeros(self):
        self.add("nu")
        self.R.tick()
        h = self.state()["tasks"]["nu"]["history"][-1]
        for k in ("model", "tokens", "session_id"):
            self.assertIn(k, h)
            self.assertIsNone(h[k], k)
        self.assertEqual(len(h["prompt_sha"]), 12)  # the runner's own fields are still there
        self.assertIn("tokens n/a", self.R.LOG_FILE.read_text(encoding="utf-8"))
        # partial usage: the key it lacks is null, a real 0 stays 0
        log = self.tmp / "partial.log"
        log.write_text(json.dumps({"usage": {"input_tokens": 5, "output_tokens": 0}}))
        self.assertEqual(self.R.lane_result(log)["tokens"],
                         {"in": 5, "out": 0, "cache_read": None, "cache_write": None})
        log.write_text("codex printed plain text and no JSON\n")  # Codex / Gemini
        self.assertEqual(self.R.lane_result(log), {})
        self.assertEqual(self.R.provenance_fields("codex", None, {}),
                         {"prompt_sha": None, "prompt_file_sha": None, "base_commit": None,
                          "lane": "codex", "model": None, "tokens": None, "session_id": None})

    def test_reconciled_run_keeps_the_provenance_from_the_lock(self):
        prov = {"prompt_sha": "a" * 12, "prompt_file_sha": "b" * 12, "base_commit": "c" * 40}
        lock = self.R.REVIEW / "runner.fake.lock"
        lock.write_text(json.dumps({"pid": 999999, "task": "rc", "lane": "fake", **prov}))
        self.add("rc")
        wt = self.R.WORKTREES / "rc"
        (wt / ".review").mkdir(parents=True)
        (wt / ".review" / "report.md").write_text("RESULT: FAIL\nHUMAN: none\nPREMISE: right\n")
        for d in (self.R.REPORTS, self.R.PARKED):  # tick() makes these before it reconciles
            d.mkdir(parents=True, exist_ok=True)
        st = self.R.load_state()
        self.assertEqual(self.R.reconcile(st), {"rc": "FAIL"})
        h = st["tasks"]["rc"]["history"][-1]
        self.assertEqual({k: h[k] for k in prov}, prov)

    def test_vault_note_shows_prompt_and_tokens(self):
        import aftercare
        v = self.tmp / "vault"
        v.mkdir()
        note = aftercare.vault_note(v, slug="s", title="T", result="PASS", lane="claude", seconds=60,
                                    spent="$0.10", prompt="abc123def456",
                                    tokens={"in": 1, "out": 2, "cache_read": 3, "cache_write": None})
        self.assertIn("prompt `abc123def456` · tokens in 1 out 2 cache-read 3 cache-write ?",
                      note.read_text(encoding="utf-8"))
        note = aftercare.vault_note(v, slug="s2", title="T", result="PASS", lane="codex", seconds=60, spent="?")
        self.assertIn("`s2` · prompt `?` · tokens n/a", note.read_text(encoding="utf-8"))

    def test_status_by_prompt_groups_the_last_14_days(self):
        import status
        status = importlib.reload(status)
        now = self.R.now()
        ago = lambda d, m=0: (now - dt.timedelta(days=d, minutes=m)).isoformat()
        state = {"tasks": {
            "a": {"history": [{"end": ago(1), "result": "PASS", "seconds": 600, "prompt_file_sha": "v2"},
                              {"end": ago(2), "result": "FAIL", "seconds": 1200, "prompt_file_sha": "v1"}]},
            "b": {"history": [{"end": ago(0, 5), "result": "PARTIAL", "seconds": 300, "prompt_file_sha": "v2"},
                              {"end": ago(0, 1), "result": "PASS", "seconds": 900, "prompt_file_sha": "v2"},
                              {"end": ago(3), "result": "NO_REPORT", "seconds": 60, "prompt_file_sha": "v1"},
                              {"end": ago(4), "result": "TIMEOUT", "seconds": 1800},
                              {"end": ago(20), "result": "PASS", "seconds": 60, "prompt_file_sha": "v2"}]}}}
        rows = {r["prompt_file_sha"]: r for r in status.by_prompt(state)}
        self.assertEqual(set(rows), {"v2", "v1", None})
        v2 = rows["v2"]
        self.assertEqual((v2["PASS"], v2["PARTIAL"], v2["FAIL"], v2["NO_REPORT"], v2["runs"]), (2, 1, 0, 0, 3),
                         "the 20-day-old run is outside the window")
        self.assertEqual(v2["median_min"], 10.0)
        v1 = rows["v1"]
        self.assertEqual((v1["PASS"], v1["FAIL"], v1["NO_REPORT"]), (0, 1, 1))
        self.assertEqual(v1["median_min"], 10.5)
        self.assertEqual((rows[None]["other"], rows[None]["runs"]), (1, 1))
        self.assertEqual(status.by_prompt(state)[0]["prompt_file_sha"], "v2", "newest-used first")
        text = status.render_by_prompt(status.by_prompt(state))
        self.assertIn("(unrecorded)", text)
        self.assertRegex(text, r"v2\s+2\s+1\s+0\s+0\s+0\s+10\.0")
        # the CLI reads state.json and exits 0, also with no state at all
        env = {**os.environ, "ORCHESTRA_REPO": str(self.repo)}
        self.R.STATE_FILE.write_text(json.dumps(state))
        r = subprocess.run([sys.executable, str(HERE / "status.py"), "--by-prompt"], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("v1", r.stdout)
        self.R.STATE_FILE.unlink()
        r = subprocess.run([sys.executable, str(HERE / "status.py"), "--by-prompt"], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no runs", r.stdout)

    def test_status_windows_follow_config(self):
        import status
        status = importlib.reload(status)
        original = {key: self.R.C[key] for key in ("status_by_prompt_days", "status_recent_runs")}
        try:
            self.R.C.update(status_by_prompt_days=3, status_recent_runs=2)
            status = importlib.reload(status)
            self.assertEqual(status.BY_PROMPT_DAYS, 3)
            self.assertEqual(status.RECENT_RUNS, 2)
            now = self.R.now()
            ago = lambda days: (now - dt.timedelta(days=days)).isoformat()
            state = {"tasks": {"a": {"history": [
                {"end": ago(1), "result": "PASS", "prompt_file_sha": "recent"},
                {"end": ago(4), "result": "PASS", "prompt_file_sha": "old"},
            ]}}}
            self.assertEqual([r["prompt_file_sha"] for r in status.by_prompt(state)], ["recent"])
            self.assertEqual(len(status.recent_runs(state)), 2)
        finally:
            self.R.C.update(original)
            importlib.reload(status)

    # ------------------------------------------------ daily exams (orchestra/exams/daily.py)

    def exam_spawns(self):
        calls = []
        return calls, lambda argv, env: calls.append((argv, env)) or 4242

    def test_exams_run_once_a_day_on_a_fake_clock(self):
        self.R.C.update(exams_daily_at="07:10", exams_agents=["codex", "copilot", "local", "claude"])
        calls, spawn = self.exam_spawns()
        state = self.R.load_state()
        day = dt.datetime(2026, 11, 3)
        self.assertIn("before 07:10", self.R.exams_step(state, at=day.replace(hour=7, minute=5), spawn=spawn))
        self.assertEqual(self.R.exams_step(state, at=day.replace(hour=7, minute=11), spawn=spawn), "started 4242")
        self.assertIn("not due", self.R.exams_step(state, at=day.replace(hour=7, minute=13), spawn=spawn))
        self.R.EXAMS_LOCK.unlink()  # that run finished
        self.assertIn("already ran today", self.R.exams_step(state, at=day.replace(hour=20), spawn=spawn))
        self.assertEqual(len(calls), 1)
        argv, env = calls[0]
        self.assertEqual(argv[1:3], ["-m", "orchestra.exams.daily"])
        self.assertEqual(argv[argv.index("--agents") + 1], "codex,copilot,local", "claude needs exams_include_claude")
        self.assertEqual(Path(argv[argv.index("--scores") + 1]), self.repo / ".review" / "exams" / "scores.json")
        self.assertEqual(env["ORCHESTRA_EXAMS_SCORES"], str(self.R.EXAMS_SCORES))
        self.assertNotIn("--vault", argv, "a test repo never writes the real vault")
        self.assertIn("EXAMS started pid=4242", self.R.LOG_FILE.read_text())
        self.assertEqual(self.R.exams_step(state, at=day.replace(day=4, hour=7, minute=10), spawn=spawn),
                         "started 4242")
        self.assertEqual(len(calls), 2)

    def test_exams_skip_while_a_lane_is_limited(self):
        self.R.C.update(exams_daily_at="07:10")
        calls, spawn = self.exam_spawns()
        state = self.R.load_state()
        state["lanes"]["codex"] = {"limited_until": (dt.datetime.now() + dt.timedelta(hours=1)).isoformat()}
        self.assertEqual(self.R.exams_step(state, at=dt.datetime.now().replace(hour=23, minute=0), spawn=spawn),
                         "not due: lane limited: codex")
        self.assertEqual(calls, [])
        self.assertFalse(self.R.EXAMS_LAST.exists(), "a skipped day is tried again once the limit lifts")

    def test_exams_respect_the_lock(self):
        self.R.C.update(exams_daily_at="00:00")
        calls, spawn = self.exam_spawns()
        self.R.EXAMS_LOCK.write_text(json.dumps({"since": dt.datetime.now().isoformat(), "token": "other"}))
        self.assertIn("lock held", self.R.exams_step(self.R.load_state(), spawn=spawn))
        self.assertEqual(calls, [])
        self.assertEqual(json.loads(self.R.EXAMS_LOCK.read_text())["token"], "other")

    def test_exams_never_block_a_tick(self):
        """A real detached child that sleeps: the tick still runs its task and returns at once,
        and the exam run holds no lane slot."""
        self.R.C.update(exams_daily_at="00:00", exams_agents=["codex"])
        self.R.exams_enabled = lambda: True
        sys.path.insert(0, str(HERE))
        from exams import daily
        pids = []

        def spawn(argv, env):
            pids.append(daily.spawn([sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.repo, env=env))
            return pids[-1]

        self.R.exams_spawn = spawn
        self.add("a")
        os.environ["FAKE_COMMIT"] = "1"
        try:
            t0 = dt.datetime.now()
            self.assertEqual(self.R.tick(), "PASS")
            self.assertLess((dt.datetime.now() - t0).total_seconds(), 55, "the tick waited for the exams")
            self.assertEqual(len(pids), 1)
            alive = subprocess.run(["tasklist", "/FI", f"PID eq {pids[0]}"], capture_output=True, text=True).stdout \
                if os.name == "nt" else ""
            if os.name == "nt":
                self.assertIn(str(pids[0]), alive, "the exam child should outlive the tick")
            self.assertTrue(self.R.EXAMS_LOCK.exists())
            self.assertEqual(list(self.R.REVIEW.glob("runner*.lock")), [], "no lane slot held")
            # the next tick: already ran today, nothing spawned, dispatch carries on
            self.add("b", title="another task")
            os.environ.pop("FAKE_COMMIT")  # done.txt is on main now; a PARTIAL still proves dispatch
            os.environ["FAKE_RESULT"] = "PARTIAL"
            self.assertEqual(self.R.tick(), "PARTIAL")
            self.assertEqual(len(pids), 1)
        finally:
            for pid in pids:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                else:
                    os.kill(pid, 9)

    def test_a_failing_exam_start_does_not_stop_dispatch(self):
        self.R.C.update(exams_daily_at="00:00")
        self.R.exams_enabled = lambda: True

        def spawn(argv, env):
            raise OSError("no python")

        self.R.exams_spawn = spawn
        self.add("a")
        os.environ["FAKE_COMMIT"] = "1"
        self.assertEqual(self.R.tick(), "PASS")
        self.assertIn("EXAMS not started", self.R.LOG_FILE.read_text())
        self.assertFalse(self.R.EXAMS_LOCK.exists(), "a failed start frees the lock")
        self.assertIn("already ran today", self.R.exams_step(self.R.load_state(), spawn=spawn))


class GuardTest(unittest.TestCase):
    main = os.path.abspath("/x/GitHub/denisse")
    wt = os.path.abspath("/x/GitHub/denisse-wt/task-1")

    def d(self, tool, **inp):
        return guard.decide({"tool_name": tool, "tool_input": inp, "cwd": self.wt}, self.wt, self.main)

    def test_writes(self):
        self.assertIsNone(self.d("Write", file_path=os.path.join(self.wt, "a.py")))
        self.assertIsNone(self.d("Edit", file_path="sub/a.py"))
        why = self.d("Write", file_path=os.path.join(self.main, "a.py"))
        self.assertIn("instead", why)
        self.assertIsNotNone(self.d("Write", file_path=os.path.abspath("/elsewhere/x.txt")))

    def test_push_only_counts_as_a_command(self):
        self.assertIsNotNone(self.d("Bash", command="git status && git push"))
        self.assertIsNotNone(self.d("Bash", command="git -c user.name=x push origin"))
        self.assertIsNone(self.d("Bash", command="cat > r.md <<'EOF'\nNot pushed (the runner merges; git push is Chuen's)\ngit push stays with Chuen\nEOF\ngit status --short"))
        self.assertIsNotNone(self.d("Bash", command="cat > r.md <<'EOF'\nx\nEOF\ngit push"))
        self.assertIsNone(self.d("Bash", command="git commit -m 'never git push from a task'"))

    def test_bash(self):
        for cmd in ("git push origin master", "git -C ../denisse commit -m x", "git reset --hard HEAD~1",
                    "git clean -fdx", "gh pr create", "rm -rf build", f"cd {self.main} && ls"):
            self.assertIsNotNone(self.d("Bash", command=cmd), cmd)
        for cmd in ("git status", "git commit -m 'fix'", ".venv/Scripts/python.exe -m pytest",
                    f"ls {self.wt}", "python scripts/test_tasks.py"):
            self.assertIsNone(self.d("Bash", command=cmd), cmd)


def make_link(src: Path, dst: Path) -> None:
    """The same kind of link the runner gives a worktree's .venv (a junction on Windows)."""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], check=True, capture_output=True)
    else:
        os.symlink(src, dst, target_is_directory=True)


class GuardLinkTest(unittest.TestCase):
    """Writes are checked on where they land, not on how the path is spelled."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.main = self.tmp / "denisse"
        self.wt = self.tmp / "denisse-wt" / "task-1"
        (self.main / ".venv" / "Scripts").mkdir(parents=True)
        (self.wt / ".review").mkdir(parents=True)
        make_link(self.main / ".venv", self.wt / ".venv")

    def tearDown(self):
        link = self.wt / ".venv"
        if os.name == "nt":
            os.rmdir(link)  # removes the junction itself, not its target
        else:
            link.unlink()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def d(self, tool, **inp):
        return guard.decide({"tool_name": tool, "tool_input": inp, "cwd": str(self.wt)},
                            str(self.wt), str(self.main))

    def test_write_through_the_venv_junction_is_denied(self):
        for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")):
            why = self.d(tool, **{key: str(self.wt / ".venv" / "Scripts" / "x.py")})
            self.assertIsNotNone(why, tool)
            self.assertIn("link", why)
        self.assertIsNotNone(self.d("Write", file_path=".venv/Lib/site.py"), "relative path via the junction")
        self.assertIsNotNone(self.d("Write", file_path=str(self.wt / ".VENV" / "x.py")), "case variant")

    def test_ordinary_writes_and_the_report_are_allowed(self):
        self.assertIsNone(self.d("Write", file_path=str(self.wt / "orchestra" / "new.py")))
        self.assertIsNone(self.d("Edit", file_path="orchestra/runner.py"))
        self.assertIsNone(self.d("Write", file_path=str(self.wt / ".review" / "report.md")))
        self.assertIsNone(self.d("Write", file_path=".review/report.md"))

    def test_running_the_venv_python_is_still_allowed(self):
        for cmd in (r".venv\Scripts\python.exe -m unittest orchestra.test_runner -v",
                    ".venv/Scripts/python.exe -m pytest"):
            self.assertIsNone(self.d("Bash", command=cmd), cmd)


SH = shutil.which("sh") or (r"C:\Program Files\Git\usr\bin\sh.exe" if os.name == "nt" else None)


class PrePushTest(unittest.TestCase):
    """orchestra/hooks/pre-push, installed by the runner into a temp main checkout.

    Hermetic: the temp repos must not see the caller's git environment. A lane that runs this
    suite can inherit GIT_DIR / GIT_INDEX_FILE (a hook, a sandboxed CLI), and a leaked
    GIT_INDEX_FILE left `main` without a commit, so the push failed with "src refspec main does
    not match any" (2026-11-09T1050, Copilot). Every GIT_* variable is dropped and the global and
    system git config are ignored for the test, then restored (testing_git.hermetic_git, shared
    with every temp-repo fixture)."""

    def setUp(self):
        testing_git.hermetic_git(self)
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.remote = self.tmp / "remote.git"
        self.wt = self.tmp / "repo-wt" / "task-1"
        self.git(self.tmp, "init", "-q", "--bare", str(self.remote))
        testing_git.init_repo(self.repo, "-b", "main")
        (self.repo / "a.txt").write_text("a")
        self.git(self.repo, "add", "a.txt")
        self.git(self.repo, "commit", "-qm", "init")
        self.git(self.repo, "remote", "add", "origin", str(self.remote))
        self.git(self.repo, "worktree", "add", "-q", "-b", "task-1", str(self.wt))
        (self.wt / "b.txt").write_text("b")
        self.git(self.wt, "add", "b.txt")
        self.git(self.wt, "commit", "-qm", "task")
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        import runner
        self.R = importlib.reload(runner)

    def tearDown(self):
        subprocess.run(["git", "worktree", "remove", "--force", str(self.wt)], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def git(cwd, *args, env=None):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)

    @staticmethod
    def env(**extra):
        e = {k: v for k, v in os.environ.items() if k != "ORCHESTRA_WORKTREE"}
        e.update(extra)
        return e

    def hook(self) -> Path:
        return Path(self.git(self.repo, "rev-parse", "--path-format=absolute", "--git-path",
                             "hooks/pre-push").stdout.strip())

    def test_runner_installs_and_refreshes_the_hook_once(self):
        self.assertTrue(self.R.install_pre_push())
        self.assertNotIn(b"\r\n", self.hook().read_bytes())
        self.assertFalse(self.R.install_pre_push(), "identical hook is left alone")
        self.hook().write_text("#!/bin/sh\nexit 0\n")
        self.assertTrue(self.R.install_pre_push(), "a stale hook is replaced")
        log = self.R.LOG_FILE.read_text(encoding="utf-8")
        self.assertEqual(log.count("pre-push hook installed"), 1)
        self.assertEqual(log.count("pre-push hook updated"), 1)

    def test_push_from_a_worktree_is_refused_with_or_without_the_env_var(self):
        self.R.install_pre_push()
        for env in (self.env(), self.env(ORCHESTRA_WORKTREE=str(self.wt))):
            r = self.git(self.wt, "push", "origin", "task-1", env=env)
            self.assertNotEqual(r.returncode, 0, r.stderr)
            self.assertIn("pre-push: refused", r.stderr)
        self.assertNotIn("task-1", self.git(self.remote, "branch").stdout)

    def test_push_from_the_main_checkout_is_allowed(self):
        self.R.install_pre_push()
        head = self.git(self.repo, "rev-parse", "main").stdout.strip()
        r = self.git(self.repo, "push", "origin", "main", env=self.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git(self.remote, "rev-parse", "--verify", "refs/heads/main").stdout.strip(), head,
                         "the remote's main is the main checkout's commit")

    def test_main_checkout_with_the_env_var_set_is_refused(self):
        self.R.install_pre_push()
        r = self.git(self.repo, "push", "origin", "main", env=self.env(ORCHESTRA_WORKTREE="x"))
        self.assertNotEqual(r.returncode, 0)

    @unittest.skipUnless(SH, "no sh to run the hook directly")
    def test_tracked_hook_is_valid_sh(self):
        r = subprocess.run([SH, "-n", str(self.R.PRE_PUSH)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
