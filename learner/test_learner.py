"""python -m unittest learner.test_learner -v  (temp vault; never touches the real one)"""
import importlib
import os
import tempfile
import unittest
from pathlib import Path


class LearnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["DENISSE_VAULT"] = str(self.tmp)
        os.environ.pop("LEARNER_STORE", None)
        from learner import core
        self.c = importlib.reload(core)
        self.d = self.c.load()

    def test_seed_is_idempotent(self):
        n = self.c.seed(self.d)
        self.assertEqual(n, len(self.c.SEED_QUESTIONS))
        self.assertEqual(self.c.seed(self.d), 0)

    def test_ask_spreads_areas_and_never_repeats_within_a_week(self):
        self.c.seed(self.d)
        first = self.c.next_questions(self.d, 3)
        self.assertEqual(len({q["area"] for q in first}), 3)
        second = self.c.next_questions(self.d, 3)
        self.assertFalse({q["id"] for q in first} & {q["id"] for q in second})

    def test_known_areas_are_asked_last(self):
        self.c.seed(self.d)
        self.c.add_belief(self.d, "Best focus in the evening", "schedule", "stated")
        picked = self.c.next_questions(self.d, 6)
        self.assertNotIn("schedule", [q["area"] for q in picked])

    def test_answer_files_a_belief_and_closes_the_question(self):
        self.c.seed(self.d)
        q = self.c.next_questions(self.d, 1)[0]
        b = self.c.answer(self.d, q["id"], "Worked example first")
        self.assertEqual(b["source"], "answered")
        self.assertEqual(b["question"], q["text"])
        self.assertEqual(next(x for x in self.d["questions"] if x["id"] == q["id"])["status"], "answered")

    def test_unsure_is_measured_not_reasked(self):
        self.c.seed(self.d)
        q = self.c.next_questions(self.d, 1)[0]
        self.assertIsNone(self.c.answer(self.d, q["id"], "", unsure=True))
        self.assertIn("To measure", self.c.render(self.d))

    def test_measured_needs_evidence_and_sources_are_fixed(self):
        with self.assertRaises(ValueError):
            self.c.add_belief(self.d, "x", "study", "measured")
        with self.assertRaises(ValueError):
            self.c.add_belief(self.d, "x", "study", "inferred")

    def test_supersede_keeps_history(self):
        b = self.c.add_belief(self.d, "Studies 25 min at a time", "study", "stated")
        n = self.c.supersede(self.d, b["id"], "Studies 45 min at a time")
        self.assertEqual(b["status"], "superseded")
        self.assertEqual(b["superseded_by"], n["id"])
        text = self.c.render(self.d)
        self.assertIn("45 min", text)
        self.assertNotIn("25 min", text)

    def test_note_has_contract_frontmatter_and_unknowns(self):
        self.c.save(self.d)
        p = self.c.write_note(self.d)
        text = p.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: Learner-Model\ndescription: "))
        self.assertIn("updated: ", text)
        self.assertIn("## Before planning anything about Chuen", text)
        self.assertIn("- unknown", text)
        self.assertTrue(str(p).startswith(str(self.tmp)))

    def test_cli_round_trip(self):
        from learner import __main__ as cli
        importlib.reload(cli)
        cli.core = self.c
        self.assertEqual(cli.main(["seed"]), 0)
        self.assertEqual(cli.main(["add", "Likes diagrams first", "--area", "format", "--source", "stated"]), 0)
        self.assertIn("Likes diagrams first", self.c.NOTE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
