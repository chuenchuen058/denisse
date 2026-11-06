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
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import consult  # noqa: E402

FAKE_DON = textwrap.dedent("""
    import json, os, sys, time
    q = sys.argv[1]
    mode = os.environ.get("FAKE_DON", "ok")
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
    print("  consult      some/note.md", file=sys.stderr)
    print(json.dumps({"question": q, "answer": "The notes say the ROC must be stated. Asked: " + q[:40],
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
        self.cfg["timeout_s"] = 2
        self.file = self.tmp / "consult.json"
        self.save()
        os.environ["ORCHESTRA_CONSULT_FILE"] = str(self.file)
        self.addCleanup(os.environ.pop, "ORCHESTRA_CONSULT_FILE", None)
        os.environ.pop("FAKE_DON", None)
        self.addCleanup(os.environ.pop, "FAKE_DON", None)
        self.log = []

    def save(self):
        self.file.write_text(json.dumps(self.cfg), encoding="utf-8")

    def task(self, text="Do the thing.", **fields):
        p = self.tmp / "t.md"
        p.write_text(f"# Make the tutor quiz shorter\nQueue: auto\n\n{text}\n", encoding="utf-8")
        return Task(p, fields=fields)

    # --- who gets consulted

    def test_why_header_paths_and_switch(self):
        self.assertIsNone(consult.why({}, "Do the thing.", self.cfg))
        self.assertIn("Consult: don", consult.why({"consult": "Don"}, "Do the thing.", self.cfg))
        self.assertIsNone(consult.why({"consult": "codex"}, "Do the thing.", self.cfg))
        for text in ("Edit tutor/chat.py so the quiz is shorter", "study_brief.py reads progress", "the learner/ store",
                     "touch `study_mode.json`", "Files: tutor\\consult.py"):
            self.assertIsNotNone(consult.why({}, text, self.cfg), text)
        for text in ("a tutorial on git", "the learners of the world", "study the runner log", "mode_tutor_x"):
            self.assertIsNone(consult.why({}, text, self.cfg), text)
        self.cfg["enabled"] = False
        self.assertIsNone(consult.why({"consult": "don"}, "tutor/chat.py", self.cfg))

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
        self.assertGreaterEqual(cfg["timeout_s"], 60, "a hand-off to Claude takes minutes")
        self.assertEqual(consult.display_name(cfg, self.repo), "Donald", "the name comes from tutor.json")
        self.assertEqual(consult.display_name(cfg, self.tmp / "nowhere"), cfg["name"])

    # --- the consult itself

    def test_before_lane_writes_the_note_and_names_it_in_the_prompt(self):
        extra = consult.before_lane(self.task(consult="don"), self.wt, self.repo, self.log.append)
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
        self.assertRegex(self.log[0], r"^CONSULT t: Donald asked \(task header Consult: don\) -> high confidence by local after \d+s")
        # The reviewer sees it, marked advisory.
        section = consult.reviewer_section(self.wt)
        self.assertIn("The notes say the ROC", section)
        self.assertIn("never a reason to block", section)

    def test_not_consulted_means_no_note_no_prompt_change_no_log(self):
        self.assertEqual(consult.before_lane(self.task(), self.wt, self.repo, self.log.append), "")
        self.assertFalse((self.wt / ".review").exists())
        self.assertEqual(self.log, [])
        self.assertEqual(consult.reviewer_section(self.wt), self.cfg["reviewer_none"])

    def test_a_tutor_that_cannot_answer_is_one_log_line_and_the_lane_runs(self):
        for mode, want in (("none", "exit 1"), ("junk", "no opinion in the output"), ("crash", "exit 2"),
                           ("sleep", "timed out after 2s")):
            os.environ["FAKE_DON"] = mode
            self.log.clear()
            self.assertEqual(consult.before_lane(self.task(text="edit tutor/chat.py"), self.wt, self.repo, self.log.append),
                             "", mode)
            self.assertFalse((self.wt / ".review" / "don.md").exists(), mode)
            self.assertEqual(len(self.log), 1, mode)
            self.assertIn("could not answer", self.log[0])
            self.assertIn(want, self.log[0], mode)
            self.assertIn("the lane runs without it", self.log[0])

    def test_missing_command_or_broken_data_file_never_raises(self):
        self.cfg["command"] = [str(self.tmp / "no-such-python.exe"), "{question}"]
        self.save()
        self.assertEqual(consult.before_lane(self.task(consult="don"), self.wt, self.repo, self.log.append), "")
        self.assertIn("not found", self.log[0])
        self.file.write_text("{ not json", encoding="utf-8")
        self.log.clear()
        self.assertEqual(consult.before_lane(self.task(consult="don"), self.wt, self.repo, self.log.append), "")
        self.assertIn("skipped", self.log[0])
        self.assertEqual(consult.reviewer_section(self.wt), "(none)")

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
