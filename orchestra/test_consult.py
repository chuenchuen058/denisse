"""Tests for orchestra/consult.py: the study tutor consulted before a lane (no model calls).

    .venv\\Scripts\\python.exe -m unittest orchestra.test_consult -v

The tutor is a fake command (FAKE_DON below) steered by the FAKE_DON env var; the data
file is a copy of consult.json with that command, pointed at by ORCHESTRA_CONSULT_FILE.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import consult  # noqa: E402

FAKE_DON = textwrap.dedent("""
    import json, os, sys, time
    q = sys.argv[1]
    notes_only = "--notes-only" in sys.argv
    mode = os.environ.get("FAKE_NOTES" if notes_only else "FAKE_DON", "ok")
    lease = os.environ.get("ORCHESTRA_GPU_LEASE")
    held = bool(lease) and os.path.exists(lease)
    if mode == "sleep":
        time.sleep(5)
    if mode == "junk":
        print("I have opinions but no JSON")
        sys.exit(0)
    if mode == "crash":
        print("Traceback: something broke", file=sys.stderr)
        sys.exit(2)
    low = {"question": q, "answer": "Nothing in the vault covers this.", "confidence": "low", "sources": [],
           "caveats": ["No notes found for the question: this is not grounded in his material."], "who": "none"}
    if mode == "none":
        print(json.dumps(low))
        sys.exit(1)
    if notes_only:
        print(json.dumps({"question": q, "answer": "- Week 5 tutor: the ROC must be stated", "confidence": "medium",
                          "sources": ["[[A/B/Week 5 tutor|Week 5 tutor]]"], "who": "notes",
                          "caveats": ["Quick answer from his notes only."]}))
        sys.exit(0)
    print("  consult      some/note.md", file=sys.stderr)
    print(json.dumps({"question": q, "answer": "The notes say the ROC must be stated. Lease held: " + str(held)
                      + ". Asked: " + q[:40],
                      "confidence": "high", "sources": ["[[A/B/Week 5 tutor|Week 5 tutor]]", "[[A/My note|My note]]"],
                      "caveats": ["No two-sided example in the notes."], "who": "local", "module": "Signals and Systems"}))
""")


@dataclass
class Task:
    path: Path
    title: str = "Make the tutor quiz shorter"
    fields: dict = field(default_factory=dict)

    @property
    def slug(self):
        return self.path.stem


class ConsultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="consult-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / "tutor").mkdir(parents=True)
        (self.repo / "tutor" / "tutor.json").write_text(json.dumps({"name": "Donald"}), encoding="utf-8")
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        self.cfg = json.loads((HERE / "consult.json").read_text(encoding="utf-8"))
        self.cfg["command"] = [sys.executable, "-c", FAKE_DON, "{question}"]
        self.cfg["notes_command"] = [sys.executable, "-c", FAKE_DON, "{question}", "--notes-only"]
        self.cfg["timeout_s"] = 2
        self.file = self.tmp / "consult.json"
        self.save()
        os.environ["ORCHESTRA_CONSULT_FILE"] = str(self.file)
        self.addCleanup(os.environ.pop, "ORCHESTRA_CONSULT_FILE", None)
        self.lease = self.tmp / "gpu.lease"
        os.environ["ORCHESTRA_GPU_LEASE"] = str(self.lease)
        self.addCleanup(os.environ.pop, "ORCHESTRA_GPU_LEASE", None)
        for k in ("FAKE_DON", "FAKE_NOTES"):
            os.environ.pop(k, None)
            self.addCleanup(os.environ.pop, k, None)
        # The shipped rule (every task), with the limits short and no real GPU probe.
        self.rule = {**consult.rules(), "consult_timeout_s": 2, "consult_notes_timeout_s": 2,
                     "consult_busy_wait_s": 0, "consult_busy_poll_s": 0}
        self.free = lambda: None
        self.log = []

    def before(self, task, busy=None, sleep=None, **rule):
        return consult.before_lane(task, self.wt, self.repo, self.log.append, rule={**self.rule, **rule},
                                   busy=busy or self.free, sleep=sleep or (lambda s: None))

    def save(self):
        self.file.write_text(json.dumps(self.cfg), encoding="utf-8")

    def task(self, text="Do the thing.", **fields):
        p = self.tmp / "t.md"
        p.write_text(f"# Make the tutor quiz shorter\nQueue: auto\n\n{text}\n", encoding="utf-8")
        return Task(p, fields=fields)

    # --- who gets consulted

    def test_why_named_rule_header_paths_and_switch(self):
        named = {"consult_applies": "named"}
        self.assertIsNone(consult.why({}, "Do the thing.", self.cfg, named))
        self.assertIn("Consult: don", consult.why({"consult": "Don"}, "Do the thing.", self.cfg, named))
        self.assertIsNone(consult.why({"consult": "codex"}, "Do the thing.", self.cfg, named))
        for text in ("Edit tutor/chat.py so the quiz is shorter", "study_brief.py reads progress", "the learner/ store",
                     "touch `study_mode.json`", "Files: tutor\\consult.py"):
            self.assertIsNotNone(consult.why({}, text, self.cfg, named), text)
        for text in ("a tutorial on git", "the learners of the world", "study the runner log", "mode_tutor_x"):
            self.assertIsNone(consult.why({}, text, self.cfg, named), text)
        self.cfg["enabled"] = False
        self.assertIsNone(consult.why({"consult": "don"}, "tutor/chat.py", self.cfg, named))
        self.assertIsNone(consult.why({}, "Do the thing.", self.cfg, {"consult_applies": "all"}))

    def test_why_all_rule_is_every_task_and_off_is_none(self):
        every = {"consult_applies": "all"}
        self.assertEqual(consult.why({}, "a tutorial on git", self.cfg, every), self.cfg["why_all"])
        self.assertIn("names tutor/", consult.why({}, "Edit tutor/chat.py", self.cfg, every),
                      "a task that names the study tooling still says so")
        self.assertIsNone(consult.why({"consult": "don"}, "tutor/chat.py", self.cfg, {"consult_applies": "off"}))
        self.assertEqual(consult.why({}, "x", self.cfg, {"consult_applies": "whatever"}), self.cfg["why_all"])

    def test_narrow_to_caps_the_rule_without_editing_it(self):
        """consult_narrow_to: named makes all mean named, off is off, "" leaves the rule alone,
        and it never widens a narrower consult_applies."""
        def why(applies, cap, fields=None, text="a tutorial on git"):
            return consult.why(fields or {}, text, self.cfg, {"consult_applies": applies, "consult_narrow_to": cap})
        self.assertEqual(why("all", ""), self.cfg["why_all"])
        self.assertIsNone(why("all", "named"), "an ordinary task is no longer put to the tutor")
        self.assertIn("Consult: don", why("all", "named", {"consult": "don"}))
        self.assertIn("names tutor/", why("all", "named", text="Edit tutor/chat.py"))
        self.assertIsNone(why("all", "off", {"consult": "don"}, "tutor/chat.py"))
        self.assertIsNone(why("off", "named", {"consult": "don"}, "tutor/chat.py"), "never widens off")
        self.assertIsNone(why("named", ""), "named stays named")
        self.assertEqual(why("all", "something else"), self.cfg["why_all"], "an unknown cap is no cap")

    def test_shipped_rule_is_narrowed_to_named_tasks(self):
        """Shipped: consult_applies is still all, capped by consult_narrow_to (docs/orchestra_vs_thomas.md)."""
        rule = consult.rules()
        self.assertEqual(rule["consult_narrow_to"], "named")
        self.assertIsNone(consult.why({}, "Fix the tray restart.", self.cfg, rule))
        self.assertIsNotNone(consult.why({}, "Edit tutor/chat.py", self.cfg, rule))
        self.assertEqual(self.before(self.task(text="Fix the tray restart."), **rule), "")
        self.assertEqual(self.log, [], "not consulted: no log line, no note")

    def test_shipped_rule_is_every_task_in_both_config_files(self):
        for name in ("config.json", "config_defaults.json"):
            data = json.loads((HERE / name).read_text(encoding="utf-8"))
            for key in consult.RULE_KEYS:
                self.assertIn(key, data, f"{key} missing from {name}")
            self.assertEqual(data["consult_applies"], "all", name)
        self.assertEqual(consult.rules()["consult_applies"], "all")
        self.assertLess(consult.rules()["consult_timeout_s"],
                        json.loads((HERE / "consult.json").read_text(encoding="utf-8"))["timeout_s"], "shorter than the old 420 s")

    def test_shipped_data_file_is_sound(self):
        cfg = json.loads((HERE / "consult.json").read_text(encoding="utf-8"))
        for key in ("enabled", "name", "header", "path_patterns", "command", "timeout_s", "question", "file",
                    "note", "prompt_line", "reviewer_section", "reviewer_none", "log", "log_failed"):
            self.assertIn(key, cfg)
        for pat in cfg["path_patterns"]:
            re.compile(pat)
        self.assertTrue(cfg["file"].startswith(".review/"), "the note stays in the worktree's private folder")
        self.assertIn("{question}", cfg["command"])
        self.assertIn("--json", cfg["command"])
        self.assertIn("--notes-only", cfg["notes_command"])
        self.assertIn("{question}", cfg["notes_command"])
        self.assertEqual(consult.display_name(cfg, self.repo), "Donald", "the name comes from tutor.json")
        self.assertEqual(consult.display_name(cfg, self.tmp / "nowhere"), cfg["name"])

    # --- the consult itself

    def test_before_lane_writes_the_note_and_names_it_in_the_prompt(self):
        extra = self.before(self.task(consult="don"))
        note = (self.wt / ".review" / "don.md").read_text(encoding="utf-8")
        self.assertIn("# Donald's opinion on this task (advisory)", note)
        self.assertIn("Confidence: **high**", note)
        self.assertIn("The notes say the ROC must be stated.", note)
        self.assertIn("- [[A/B/Week 5 tutor|Week 5 tutor]]", note)
        self.assertIn("- No two-sided example in the notes.", note)
        self.assertIn("Make the tutor quiz shorter", note, "the question carries the task title")
        self.assertIn("Do the thing.", note, "and its text")
        self.assertIn("task header Consult: don", note)
        self.assertIn("`.review/don.md`", extra)
        self.assertIn("Donald", extra)
        self.assertIn("confidence: high", extra)
        self.assertIn("never an override", extra)
        self.assertEqual(len(self.log), 1)
        self.assertRegex(self.log[0], r"^CONSULT t: Donald asked \(task header Consult: don\) path=model -> high confidence "
                                      r"by local after \d+s \(the full consult\)")
        self.assertIn("Lease held: True", note, "the full consult holds the GPU lease while it runs")
        self.assertFalse(self.lease.exists(), "and releases it")
        # The reviewer sees it, marked advisory.
        section = consult.reviewer_section(self.wt)
        self.assertIn("The notes say the ROC", section)
        self.assertIn("never a reason to block", section)

    def test_not_consulted_means_no_note_no_prompt_change_no_log(self):
        self.assertEqual(self.before(self.task(), consult_applies="named"), "")
        self.assertFalse((self.wt / ".review").exists())
        self.assertEqual(self.log, [])
        self.assertEqual(consult.reviewer_section(self.wt), self.cfg["reviewer_none"])

    def test_a_tutor_that_cannot_answer_is_one_log_line_and_the_lane_runs(self):
        os.environ["FAKE_NOTES"] = "crash"
        for mode, want in (("none", "exit 1"), ("junk", "no opinion in the output"), ("crash", "exit 2"),
                           ("sleep", "timed out after 2s")):
            os.environ["FAKE_DON"] = mode
            self.log.clear()
            self.assertEqual(self.before(self.task(text="edit tutor/chat.py")), "", mode)
            self.assertFalse((self.wt / ".review" / "don.md").exists(), mode)
            self.assertEqual(len(self.log), 1, mode)
            self.assertIn("could not answer", self.log[0])
            self.assertIn(want, self.log[0], mode)
            self.assertIn("the lane runs without it", self.log[0])
            self.assertIn("notes-only also failed", self.log[0], mode)

    def test_missing_command_or_broken_data_file_never_raises(self):
        self.cfg["command"] = [str(self.tmp / "no-such-python.exe"), "{question}"]
        self.cfg["notes_command"] = [str(self.tmp / "no-such-python.exe"), "{question}", "--notes-only"]
        self.save()
        self.assertEqual(self.before(self.task(consult="don")), "")
        self.assertIn("not found", self.log[0])
        self.file.write_text("{ not json", encoding="utf-8")
        self.log.clear()
        self.assertEqual(self.before(self.task(consult="don")), "")
        self.assertIn("skipped", self.log[0])
        self.assertEqual(consult.reviewer_section(self.wt), "(none)")

    # --- every task, and the busy GPU

    def test_every_task_is_consulted_with_the_general_question(self):
        # consult_applies all, not narrowed (shipped: consult_narrow_to "named", tested below).
        extra = self.before(self.task(text="Fix the tray restart."), consult_narrow_to="")
        self.assertIn("`.review/don.md`", extra)
        note = (self.wt / ".review" / "don.md").read_text(encoding="utf-8")
        self.assertIn(self.cfg["why_all"], note)
        self.assertIn("in Chuen's Denisse repo", note, "the general question, not the study one")
        self.assertIn("path=model", self.log[0])

    def test_busy_gpu_is_waited_for_then_the_full_consult_runs(self):
        answers, slept = ["the GPU lease is held by eval", None], []
        self.assertTrue(self.before(self.task(consult="don"), busy=lambda: answers.pop(0) if answers else None,
                                    sleep=slept.append, consult_busy_wait_s=60, consult_busy_poll_s=7))
        self.assertEqual(slept, [7])
        self.assertIn("path=model", self.log[0])

    def test_gpu_that_stays_busy_takes_the_notes_only_path(self):
        clock = iter(range(0, 1000, 30))
        slept = []
        with unittest.mock.patch.object(consult.time, "monotonic", lambda: next(clock)):
            extra = self.before(self.task(consult="don"), busy=lambda: "the GPU is 97% busy (limit 60%)",
                                sleep=slept.append, consult_busy_wait_s=60, consult_busy_poll_s=30)
        self.assertIn("confidence: medium", extra)
        self.assertTrue(slept, "it waited before giving up")
        note = (self.wt / ".review" / "don.md").read_text(encoding="utf-8")
        self.assertIn("Answered by: notes (notes only: the GPU is 97% busy (limit 60%) after waiting 60s)", note)
        self.assertIn("path=notes", self.log[0])
        self.assertIn("by notes", self.log[0])

    def test_lease_held_by_another_job_counts_as_busy(self):
        self.lease.write_text(json.dumps({"pid": os.getpid(), "holder": "local team scout", "ttl_s": 600,
                                          "since": consult.dt.datetime.now().replace(microsecond=0).isoformat()}),
                              encoding="utf-8")
        self.assertEqual(consult.gpu_busy(self.repo, self.rule, self.cfg), "the GPU lease is held by local team scout")
        self.lease.unlink()
        with unittest.mock.patch("local_team.core.gpu_utilisation", return_value=None):
            self.assertIsNone(consult.gpu_busy(self.repo, self.rule, self.cfg))
        with unittest.mock.patch("local_team.core.gpu_utilisation", return_value=80):
            self.assertEqual(consult.gpu_busy(self.repo, self.rule, self.cfg), "the GPU is 80% busy (limit 60%)")

    def test_a_consult_that_loses_the_lease_race_queues_behind_it(self):
        """One full consult at a time: a lease another consult took between the busy check
        and here is waited for (up to consult_busy_wait_s), not a jump to notes-only; and the
        tutor it runs is told the lease is its own (local_llm owner env)."""
        class Lease:
            released = 0

            def release(self):
                Lease.released += 1

        slept, asked = [], []

        def ask(repo, q, cfg, command="command", timeout_s=None, env=None):
            asked.append((command, env))
            return {"ok": True, "result": {"answer": "a", "confidence": "high"}, "why": "", "seconds": 0}

        rule = {**self.rule, "consult_busy_wait_s": 30, "consult_busy_poll_s": 5}
        with unittest.mock.patch.object(consult, "_lease", side_effect=[False, False, Lease()]),                 unittest.mock.patch.object(consult, "ask", ask):
            r = consult.opinion(self.repo, "q", self.cfg, rule, busy=self.free, sleep=slept.append)
        self.assertEqual(r["path"], "model")
        self.assertEqual(slept, [5, 5])
        self.assertEqual(Lease.released, 1)
        self.assertEqual(asked[0][0], "command")
        self.assertEqual(asked[0][1], consult._lease_owner_env())
        self.assertTrue(asked[0][1], "the owner env names this process")

    def test_a_lease_still_lost_after_the_wait_is_notes_only(self):
        def ask(repo, q, cfg, command="command", timeout_s=None, env=None):
            return {"ok": True, "result": {"answer": command, "confidence": "low"}, "why": "", "seconds": 0}

        rule = {**self.rule, "consult_busy_wait_s": 10, "consult_busy_poll_s": 5}
        with unittest.mock.patch.object(consult, "_lease", return_value=False),                 unittest.mock.patch.object(consult, "ask", ask):
            r = consult.opinion(self.repo, "q", self.cfg, rule, busy=self.free, sleep=lambda s: None)
        self.assertEqual(r["path"], "notes")
        self.assertIn("after waiting 10s", r["how"])

    def test_a_failed_full_consult_falls_back_to_notes(self):
        os.environ["FAKE_DON"] = "sleep"
        extra = self.before(self.task(consult="don"))
        self.assertIn("confidence: medium", extra)
        self.assertIn("path=notes", self.log[0])
        self.assertIn("the full consult failed (timed out after 2s)", self.log[0])
        self.assertFalse(self.lease.exists(), "the lease is released after a timeout too")

    def test_notes_fallback_off_logs_and_runs_without(self):
        os.environ["FAKE_DON"] = "crash"
        self.assertEqual(self.before(self.task(consult="don"), consult_notes_fallback=False), "")
        self.assertIn("could not answer", self.log[0])
        self.assertIn("exit 2", self.log[0])

    def test_no_tutor_in_the_checkout_is_one_log_line(self):
        shutil.rmtree(self.repo / "tutor")
        self.assertEqual(self.before(self.task(consult="don")), "")
        self.assertEqual(len(self.log), 1)
        self.assertIn("no tutor in this checkout", self.log[0])

    def test_python_placeholder_prefers_the_repo_venv(self):
        self.assertEqual(consult._python(self.repo, self.cfg), sys.executable)
        venv = self.repo / self.cfg["python"]
        venv.parent.mkdir(parents=True, exist_ok=True)
        venv.write_text("", encoding="utf-8")
        self.assertEqual(consult._python(self.repo, self.cfg), str(venv))

    def test_question_is_capped(self):
        self.cfg["question_chars"] = 20
        q = consult.question("T", "x" * 100, self.cfg)
        self.assertIn("x" * 20, q)
        self.assertNotIn("x" * 21, q)


if __name__ == "__main__":
    unittest.main()
