"""study_brief.json drives the brief: change the data, the brief changes.

python -m unittest discover -s scripts -p "test_study_brief_data.py" -v

No real vault, learner store or task list: beliefs are passed straight to
learner_shape, and the job is the same fixture test_study_brief_learner uses.
"""
import copy
import datetime as dt
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import study_brief as sb  # noqa: E402
from test_study_brief_learner import FAKE_VAULT, GOLDEN_QUESTIONS, TODAY, fixture_job  # noqa: E402


def belief(claim, area, bid="b1"):
    return {"id": bid, "claim": claim, "area": area, "status": "current"}


class Data(unittest.TestCase):
    def setUp(self):
        self._vault, sb.VAULT = sb.VAULT, FAKE_VAULT

    def tearDown(self):
        sb.VAULT = self._vault

    def brief(self, beliefs):
        return sb._brief_text(fixture_job(True), TODAY, sb.learner_shape(beliefs))

    def test_default_data_unchanged(self):
        self.assertEqual(self.brief([]), GOLDEN_QUESTIONS)

    def test_a_new_format_kind_in_the_data_shapes_the_brief(self):
        learn = copy.deepcopy(sb._LEARN)
        learn["format_first"]["audio"] = {"pattern": r"(?i)\bpodcasts?\b", "shape": "audio_first",
                                          "revise_first": r"(?i)\btutorial\b"}
        brief = copy.deepcopy(sb._B)
        brief["audio_first"] = ["Start each item from the lecture recording.", ""]
        brief["shaped"]["audio_first"] = "- Recording before reading — {cite}"
        with mock.patch.object(sb, "_LEARN", learn), mock.patch.object(sb, "_B", brief):
            text = self.brief([belief("A podcast first, then notes", "format")])
        self.assertIn("Start each item from the lecture recording.", text)
        self.assertIn("- Recording before reading — b1:", text)
        self.assertIn("| Q2 three plots | 16 | Wk 4 tutorial sheet; Wk 4 lecture (FT) |", text)
        self.assertNotIn("Start each item from a worked example", text)

    def test_revise_first_pattern_is_data(self):
        learn = copy.deepcopy(sb._LEARN)
        learn["format_first"]["example"]["revise_first"] = r"(?i)\blecture\b"
        with mock.patch.object(sb, "_LEARN", learn):
            text = self.brief([belief("Worked example first", "format")])
        # lecture now leads, tutorial after: the reorder follows the data, not the code
        self.assertIn("| Q2 three plots | 16 | Wk 4 lecture (FT); Wk 4 tutorial sheet |", text)

    def test_a_format_kind_set_to_null_changes_nothing(self):
        learn = copy.deepcopy(sb._LEARN)
        learn["format_first"]["example"]["shape"] = None
        with mock.patch.object(sb, "_LEARN", learn):
            self.assertEqual(self.brief([belief("Worked example first", "format")]), GOLDEN_QUESTIONS)

    def test_shaped_footer_order_follows_the_data(self):
        brief = copy.deepcopy(sb._B)
        brief["shaped"] = dict(reversed(list(brief["shaped"].items())))
        beliefs = [belief("Worked example first", "format", "b1"), belief("Gets distracted", "attention", "b2")]
        with mock.patch.object(sb, "_B", brief):
            footer = self.brief(beliefs).split("## Shaped by")[1]
        self.assertLess(footer.index("b2:"), footer.index("b1:"))

    def test_pace_bands_are_data(self):
        learn = copy.deepcopy(sb._LEARN)
        learn["pace"] = [[120, 4]]
        with mock.patch.object(sb, "_LEARN", learn):
            text = self.brief([belief("45 minutes", "study")])
        self.assertIn("- 4 plan item(s) per session", text)

    def test_schedule_day_counts_are_data(self):
        today, due = dt.date(2026, 11, 1), dt.date(2026, 11, 21)
        work = [{"text": "Q1", "weight": 10}]
        self.assertEqual(sb.schedule(work, today, due)[0][0], dt.date(2026, 11, 19))
        with mock.patch.dict(sb._S, work_ends_days_before=5, submit_days_before=3):
            plan = sb.schedule(work, today, due)
        self.assertEqual(plan[0][0], dt.date(2026, 11, 16))
        self.assertEqual(plan[-1][0], dt.date(2026, 11, 18))

    def test_job_file_slug_separator_is_data(self):
        self.assertTrue(sb._job_file("Signals and Systems").endswith("study_brief-signals-and-systems.json"))
        with mock.patch.dict(sb._PATHS, slug_sep="_"):
            self.assertTrue(sb._job_file("Signals and Systems").endswith("study_brief-signals_and_systems.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
