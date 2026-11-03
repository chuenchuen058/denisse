"""study_brief.py + the learner model: beliefs shape the brief, deterministically.

python -m unittest discover -s scripts -p "test_study_brief_learner.py" -v

Every test uses a temp learner store and a temp vault; the real ones (his
vault, Dev/learner/model.json, memory/jobs, his task list) are never read or
written. GOLDEN_* are the briefs exactly as study_brief wrote them before the
learner model was wired in -- no beliefs must still give those, byte for byte.
"""
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import planner  # noqa: E402
import study_brief as sb  # noqa: E402
import tasks  # noqa: E402
from learner import core as learner_core  # noqa: E402

TODAY = dt.date(2026, 10, 30)
FAKE_VAULT = os.path.join(tempfile.gettempdir(), "denisse-fake-vault")

GOLDEN_QUESTIONS = (
    '---\nname: 2026-10-30 Signals and Systems study brief\ndescription: "Study brief for Signals and Systems: coursework due 24 Nov 2026, 3 of 4 items left. Built by Denisse from the module notes."\ntags: [denisse, draft, study-brief]\nupdated: 2026-10-30\n---\n\n# Signals and Systems \u2014 study brief\n\n> [!important] Due **Tue 24 Nov 2026** (in 25 days) \xb7 30%\n> From [[Atlas/Mod/Coursework.md|the coursework note]]. Built 30 Oct from your notes only.\n\n## Where you are\n\n- **1 of 4** coursework items ticked.\n- Not written up yet: [[SS Week 4|Week 4]] (scaffolded).\n- On your task list already: 1 \u2014 Signals and Systems: email the TA about Q3.\n\n## What\'s left\n\n| Item | Marks | Revise from |\n|---|---|---|\n| Q2 three plots | 16 | Wk 4 lecture (FT); Wk 4 tutorial sheet |\n| Q3 (i)-(iv) | 10 | Laplace lecture (later week) |\n| Assemble single report, submit |  |  |\n\n## Plan\n\nWeek 4 isn\'t written up, and questions below need them -- do the write-up before the question.\n\n- by **Tue 03 Nov** \u2014 Write up Week 4 (Topic 4)\n- by **Thu 12 Nov** \u2014 Q2 three plots (Fourier transform, 16 marks)\n- by **Wed 18 Nov** \u2014 Q3 (i)-(iv) (Laplace properties, 10 marks)\n- by **Sun 22 Nov** \u2014 Assemble single report, submit\n- by **Mon 23 Nov** \u2014 Assemble, check and submit on KEATS\n\n## What I don\'t know\n\n- Not linked to a taught week yet: Q3 (i)-(iv).\n- Your lecture timetable and exam dates \u2014 the plan covers this coursework only.\n')
GOLDEN_PLAN_LIST = (
    '---\nname: 2026-10-30 Electromagnetism study brief\ndescription: "Study brief for Electromagnetism: coursework due 24 Nov 2026, 3 of 3 items left. Built by Denisse from the module notes."\ntags: [denisse, draft, study-brief]\nupdated: 2026-10-30\n---\n\n# Electromagnetism \u2014 study brief\n\n> [!important] Due **Tue 24 Nov 2026** (in 25 days) \xb7 30%\n> From [[Atlas/Mod/Coursework.md|the coursework note]]. Built 30 Oct from your notes only.\n\n## Where you are\n\n- **0 of 3** coursework items ticked.\n- **The brief isn\'t read yet** \u2014 the plan below is guessed from the deadline until it is.\n- Not written up yet: [[SS Week 4|Week 4]] (scaffolded).\n\n## What\'s left\n\n- [ ] Read the brief and replace the guesses above\n- [ ] Draft\n- [ ] Submit on KEATS\n\n## Plan\n\n- by **Mon 02 Nov** \u2014 Read the Electromagnetism coursework brief\n- by **Sun 15 Nov** \u2014 Draft\n- by **Mon 23 Nov** \u2014 Assemble, check and submit on KEATS\n\n## What I don\'t know\n\n- Your lecture timetable and exam dates \u2014 the plan covers this coursework only.\n')


def fixture_job(questions: bool) -> dict:
    """A finished-steps job, as run() would have it just before writing."""
    if questions:
        items = [{"text": "Q1 sketches", "done": True, "question": "1", "topic": "Signals & the Dirac delta",
                  "marks": 14, "revise": "Wk 1 lecture; Wk 3 tutorial", "weeks": [1, 3]},
                 {"text": "Q2 three plots", "done": False, "question": "2", "topic": "Fourier transform",
                  "marks": 16, "revise": "Wk 4 lecture (FT); Wk 4 tutorial sheet", "weeks": [4]},
                 {"text": "Q3 (i)-(iv)", "done": False, "question": "3", "topic": "Laplace properties",
                  "marks": 10, "revise": "Laplace lecture (later week)", "weeks": []},
                 {"text": "Assemble single report, submit", "done": False}]
        cw = {"items": items, "feeds": [], "has_questions": True}
        plan = [("2026-11-03", "Write up Week 4 (Topic 4)"),
                ("2026-11-12", "Q2 three plots (Fourier transform, 16 marks)"),
                ("2026-11-18", "Q3 (i)-(iv) (Laplace properties, 10 marks)"),
                ("2026-11-22", "Assemble single report, submit"),
                ("2026-11-23", "Assemble, check and submit on KEATS")]
        job_tasks, unread = [{"text": "Signals and Systems: email the TA about Q3", "due": ""}], False
    else:
        cw = {"items": [{"text": "Read the brief and replace the guesses above", "done": False},
                        {"text": "Draft", "done": False}, {"text": "Submit on KEATS", "done": False}],
              "feeds": [1, 2], "has_questions": False}
        plan = [("2026-11-02", "Read the Electromagnetism coursework brief"), ("2026-11-15", "Draft"),
                ("2026-11-23", "Assemble, check and submit on KEATS")]
        job_tasks, unread = [], True
    return {"module": "Signals and Systems" if questions else "Electromagnetism",
            "done": {"deadline": {"due": "2026-11-24", "weight": "30%", "brief_unread": unread,
                                  "path": os.path.join(FAKE_VAULT, "Atlas", "Mod", "Coursework.md")},
                     "coursework": cw,
                     "weeks": [{"week": 1, "topic": "T1", "note": "SS Week 1", "written": True, "status": "written"},
                               {"week": 4, "topic": "T4", "note": "SS Week 4", "written": False,
                                "status": "scaffolded"}],
                     "tasks": job_tasks, "plan": plan}}


class LearnerStore(unittest.TestCase):
    """A temp learner store; beliefs go in through learner.core, as the CLI does."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="denisse-brief-learner-")
        self._store, self._vault = learner_core.STORE, sb.VAULT
        learner_core.STORE = type(learner_core.STORE)(os.path.join(self.tmp, "learner", "model.json"))
        sb.VAULT = FAKE_VAULT
        self.data = learner_core.load()

    def tearDown(self):
        learner_core.STORE, sb.VAULT = self._store, self._vault

    def believe(self, claim, area, source="stated", evidence=""):
        b = learner_core.add_belief(self.data, claim, area, source, evidence)
        learner_core.save(self.data)
        return b

    def brief(self, questions=True):
        return sb._brief_text(fixture_job(questions), TODAY, sb.learner_shape(sb.learner_beliefs()))


class NoBeliefs(LearnerStore):
    def test_no_store_is_todays_brief_exactly(self):
        self.assertFalse(learner_core.STORE.exists())
        self.assertEqual(sb.learner_beliefs(), [])
        self.assertEqual(self.brief(True), GOLDEN_QUESTIONS)
        self.assertEqual(self.brief(False), GOLDEN_PLAN_LIST)
        self.assertFalse(learner_core.STORE.exists(), "reading must not create the store")

    def test_empty_store_is_todays_brief_exactly(self):
        learner_core.seed(self.data)  # questions only, no beliefs
        learner_core.save(self.data)
        self.assertEqual(self.brief(True), GOLDEN_QUESTIONS)
        self.assertEqual(self.brief(False), GOLDEN_PLAN_LIST)

    def test_broken_store_is_todays_brief(self):
        learner_core.STORE.parent.mkdir(parents=True)
        learner_core.STORE.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.brief(True), GOLDEN_QUESTIONS)

    def test_no_shape_argument_is_todays_brief(self):
        self.assertEqual(sb._brief_text(fixture_job(True), TODAY), GOLDEN_QUESTIONS)


class EachArea(LearnerStore):
    def test_format_worked_example_first(self):
        b = self.believe("Worked example first, then the theory", "format")
        text = self.brief(True)
        self.assertIn("Start each item from a worked example", text)
        self.assertLess(text.index("Start each item from a worked example"), text.index("| Item | Marks |"))
        # the tutorial (worked examples) now comes before the lecture (theory)
        self.assertIn("| Q2 three plots | 16 | Wk 4 tutorial sheet; Wk 4 lecture (FT) |", text)
        self.assertIn(f"- Worked example before theory — {b['id']}:", text)

    def test_format_worked_example_without_a_table(self):
        self.believe("A worked example before anything else", "format")
        text = self.brief(False)
        self.assertLess(text.index("Start each item from a worked example"), text.index("- [ ] Draft"))

    def test_format_diagram_first(self):
        b = self.believe("A picture or diagram first", "format")
        text = self.brief(True)
        self.assertIn("Start each item from a sketch or diagram", text)
        self.assertIn(f"- Picture before theory — {b['id']}:", text)
        self.assertNotIn("Start each item from a worked example", text)

    def test_format_theory_first_changes_nothing(self):
        self.believe("The theory and derivation first, then a worked example", "format")
        self.believe("Short bullet points", "format")
        self.assertEqual(self.brief(True), GOLDEN_QUESTIONS)

    def test_attention_shortens_sections(self):
        b = self.believe("Phone notifications pull me off task", "attention")
        text = self.brief(True)
        body = text.split("\n## Shaped by")[0]
        self.assertLess(len(body), len(GOLDEN_QUESTIONS))
        self.assertIn("- On your task list already: 1.\n", text)
        self.assertNotIn("email the TA", text)
        self.assertNotIn("(scaffolded)", text)
        self.assertIn("Write up Week 4 before the questions that need it.", text)
        self.assertIn(f"- Shorter sections — {b['id']}:", text)
        # shorter, never lossier: every unticked item and plan step is still there
        job = fixture_job(True)
        for i in job["done"]["coursework"]["items"]:
            if not i["done"]:
                self.assertIn(i["text"], text)
        for _, step in job["done"]["plan"]:
            self.assertIn(step, text)

    def test_pace_sets_items_per_session(self):
        for claim, per in (("About 25 minutes before a break", 1), ("45 min", 2), ("2 hours in one go", 3)):
            with self.subTest(claim=claim):
                self.data = {"beliefs": [], "questions": []}
                b = self.believe(claim, "study")
                text = self.brief(True)
                self.assertIn("## Sessions", text)
                sessions = [line for line in text.splitlines() if line.startswith("- Session ")]
                self.assertEqual(len(sessions), -(-5 // per), sessions)  # 5 plan steps
                self.assertEqual(sessions[0].count(";"), per - 1)
                self.assertIn(f"- {per} plan item(s) per session", text)
                self.assertIn(f"{b['id']}:", text.split("## Shaped by")[1])

    def test_study_belief_without_a_length_changes_nothing(self):
        self.believe("Studies best in the library", "study")
        self.assertEqual(self.brief(True), GOLDEN_QUESTIONS)

    def test_latest_pace_wins(self):
        self.believe("25 minutes at a time", "study")
        self.believe("Now 90 minutes at a time", "study")
        self.assertIn("About 90 min a session", self.brief(True))


class Footer(LearnerStore):
    def test_footer_lists_every_belief_used_and_only_those(self):
        ids = [self.believe("Worked example first", "format")["id"],
               self.believe("Gets distracted by messages", "attention")["id"],
               self.believe("45 minutes per session", "study")["id"]]
        unused = self.believe("A first in Signals and Systems", "goals")["id"]
        text = self.brief(True)
        footer = text.split("\n## Shaped by\n")[1]
        for i in ids:
            self.assertIn(f"{i}:", footer)
        self.assertNotIn(f"{unused}:", footer)
        self.assertEqual(text.count("## Shaped by"), 1)
        self.assertTrue(text.rstrip().endswith("ask again."))

    def test_unknown_and_unused_areas_are_ignored(self):
        for area in ("goals", "schedule", "struggles", "feedback"):
            self.believe(f"Something about {area}: worked example, 30 min", area)
        # an area the learner model doesn't have, written straight into the store
        self.data["beliefs"].append({"id": "b99", "claim": "Worked example, 20 min", "area": "pace",
                                     "source": "stated", "status": "current"})
        learner_core.save(self.data)
        self.assertEqual(self.brief(True), GOLDEN_QUESTIONS)

    def test_superseded_beliefs_are_ignored(self):
        b = self.believe("Worked example first", "format")
        learner_core.supersede(self.data, b["id"], "Theory first, please")
        learner_core.save(self.data)
        self.assertEqual(self.brief(True), GOLDEN_QUESTIONS)

    def test_same_beliefs_same_brief(self):
        self.believe("Worked example first", "format")
        self.believe("45 minutes", "study")
        self.assertEqual(self.brief(True), self.brief(True))


class ReadOnly(LearnerStore):
    def test_the_store_is_never_written(self):
        self.believe("Worked example first", "format")
        self.believe("45 minutes", "study")
        before = hashlib.sha256(learner_core.STORE.read_bytes()).hexdigest()
        mtime = learner_core.STORE.stat().st_mtime_ns
        self.brief(True)
        self.brief(False)
        sb.learner_shape(sb.learner_beliefs())
        self.assertEqual(hashlib.sha256(learner_core.STORE.read_bytes()).hexdigest(), before)
        self.assertEqual(learner_core.STORE.stat().st_mtime_ns, mtime)
        self.assertEqual(len(json.loads(learner_core.STORE.read_text(encoding="utf-8"))["beliefs"]), 2)


class WholeJob(LearnerStore):
    """run() end to end: the shaped brief is written and passes its own check."""

    def setUp(self):
        super().setUp()
        self._saved = (tasks.TASKS_FILE, tasks._mirror_to_vault, planner.PENDING_FILE, planner.VAULT,
                       planner.coursework_deadlines, sb.DRAFTS, sb.JOBS_DIR)
        vault = os.path.join(self.tmp, "vault")
        tasks.TASKS_FILE = os.path.join(self.tmp, "tasks.json")
        tasks._mirror_to_vault = lambda: None
        planner.PENDING_FILE = os.path.join(self.tmp, "pending_plan.json")
        sb.VAULT = planner.VAULT = vault
        sb.DRAFTS = os.path.join(vault, "Denisse", "Drafts")
        sb.JOBS_DIR = os.path.join(self.tmp, "jobs")
        orig = self._saved[4]
        planner.coursework_deadlines = lambda v=vault: orig(v)
        ss = os.path.join(vault, "Atlas", "Academic", "SEM 1 Modules", "5CCYB020 Signals and Systems")
        self.note(os.path.join(ss, "Coursework", "Coursework.md"), (
            "---\ndescription: \"5CCYB020 summative coursework (30%)\"\n---\n"
            "> [!important] Due **Tuesday 24 November 2026, 16:00** on KEATS.\n\n"
            "| Q | Topic | Marks | Revise from |\n|---|---|---|---|\n"
            "| **1** | Signals — sketch | 14 | Wk 1 lecture; Wk 3 tutorial |\n"
            "| **2** | Fourier transform — plots | 16 | Wk 4 lecture (FT); Wk 4 tutorial |\n\n"
            "## Progress\n\n- [x] Q1 sketches\n- [ ] Q2 three plots\n- [ ] Assemble single report, submit\n"))
        for n, status in ((1, ""), (3, ""), (4, "scaffolded")):
            self.note(os.path.join(ss, f"Week {n}", f"Signals and Systems Week {n}.md"),
                      f"---\nweek: {n}\ntopic: Topic {n}\n" + (f"status: {status}\n" if status else "") + "---\n")

    def tearDown(self):
        (tasks.TASKS_FILE, tasks._mirror_to_vault, planner.PENDING_FILE, planner.VAULT,
         planner.coursework_deadlines, sb.DRAFTS, sb.JOBS_DIR) = self._saved
        super().tearDown()

    @staticmethod
    def note(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_shaped_brief_is_written_and_verified(self):
        ids = [self.believe("Worked example first", "format")["id"],
               self.believe("Gets distracted by messages", "attention")["id"],
               self.believe("45 minutes per session", "study")["id"]]
        before = learner_core.STORE.read_bytes()
        reply = sb.run("prep me for signals and systems", dt.datetime(2026, 10, 30, 19, 0))
        self.assertTrue(reply.startswith("Your Signals and Systems study brief"), reply)
        path = os.path.join(sb.DRAFTS, "2026-10-30 Signals and Systems study brief.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        footer = text.split("\n## Shaped by\n")[1]
        for i in ids:
            self.assertIn(f"{i}:", footer)
        self.assertIn("| Q2 three plots | 16 | Wk 4 tutorial; Wk 4 lecture (FT) |", text)
        self.assertIn("## Sessions", text)
        with open(sb._job_file("Signals and Systems"), encoding="utf-8") as f:
            job = json.load(f)
        self.assertEqual(job["status"], "done")
        self.assertEqual(sb.verify(path, job, TODAY), [])
        self.assertEqual(learner_core.STORE.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
