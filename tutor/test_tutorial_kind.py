"""The tutorial card kind: problems in the style of the module's own problem-class sheet, full worked solutions."""
from __future__ import annotations

import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import cards, conf  # noqa: E402
from tutor.test_don import DonFixture, fake_cards, passing_checks  # noqa: E402
from tutor.test_tutor import MODULES, SS  # noqa: E402

CFG = conf.CONFIG
STYLED = [k for k, v in CFG["cards"]["kinds"].items() if v.get("style")]
SHEET = ("Problem Class 5 Laplace\n\n1. A causal system has impulse response h(t) = e^{-2t}u(t). "
         "(i) Find H(s). (ii) Find the step response. (iii) State its final value.\n")


class TutorialKind(DonFixture):
    def setUp(self):
        super().setUp()
        self.assertTrue(STYLED, "tutor.json has a style-matched kind")
        self.kind = STYLED[0]
        self.note(os.path.join(MODULES, SS, "Week 5", "Tutorial - Questions.txt"), SHEET)

    def test_new_kind_is_added_to_an_existing_deck_without_remaking_the_rest(self):
        self.run_ingest()
        saved = CFG["cards"]["kinds"].pop(self.kind)
        try:
            out1, _ = self.make_cards()
        finally:
            CFG["cards"]["kinds"][self.kind] = saved
        self.assertNotIn(self.kind, {c["kind"] for c in out1["cards"]})
        log = []
        out2, _ = self.make_cards(log=log)
        asked = {tag for tag, _user in log}
        self.assertEqual(asked, {CFG["cards"]["kinds"][self.kind]["engine"]}, "only the new kind's engine was asked")
        self.assertTrue(all(f'(kind: "{self.kind}")' in user and '(kind: "recall")' not in user for _t, user in log))
        self.assertIn(self.kind, {c["kind"] for c in out2["cards"]})
        old = {c["id"] for c in out1["cards"]}
        self.assertTrue(old <= {c["id"] for c in out2["cards"]}, "the earlier cards keep their ids")
        out3, log3 = self.make_cards()
        self.assertEqual(log3, [], "asked once: a third run asks nothing")

    def test_prompt_carries_the_sheet_as_a_style_example(self):
        self.run_ingest()
        log = []
        self.make_cards(log=log)
        styled = [u for _t, u in log if f'(kind: "{self.kind}")' in u]
        self.assertTrue(styled)
        self.assertTrue(any("Problem Class 5 Laplace" in u for u in styled), "the module's sheet is in the prompt")
        plain = [u for _t, u in log if f'(kind: "{self.kind}")' not in u]
        self.assertTrue(all("Problem Class 5" not in u for u in plain), "other kinds don't pay for the sheet")

    def test_worked_solutions_get_their_own_length_and_check_note(self):
        self.run_ingest()
        long_back = "Set-up: axes.\n(i) " + "step " * 400 + "\nAnswer: 1/(s+2)\nCheck: units."
        engine = CFG["cards"]["kinds"][self.kind]["engine"]

        def writer(system, user):
            import re
            out = [{"kind": k, "front": f"{k} q", "back": long_back if k == self.kind else "short", "topic": "t"}
                   for k in re.findall(r'\(kind: "(\w+)"\)', user)]
            return json.dumps({"cards": out})
        calls = {"local": fake_cards([]), "claude": fake_cards([], "claude")}
        calls[engine] = writer
        check_log = []
        out, _ = self.make_cards(calls=calls, checks=passing_checks(check_log))
        tut = [c for c in out["cards"] if c["kind"] == self.kind]
        self.assertTrue(tut)
        self.assertGreater(len(tut[0]["back"]), CFG["cards"]["back_chars"])
        self.assertTrue(tut[0]["back"].rstrip().endswith("Check: units."))
        note = CFG["cards"]["kinds"][self.kind]["check_note"]
        self.assertTrue(any(note in u for _t, u in check_log), "the checker is told it is a new problem")


if __name__ == "__main__":
    unittest.main(verbosity=2)
