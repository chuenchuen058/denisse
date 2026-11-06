"""The checker turns a card whose front is only a name into a full question, and a re-run keeps it."""
from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import conf  # noqa: E402
from tutor.test_don import DonFixture, fake_check  # noqa: E402

CFG = conf.CONFIG
K = CFG["cards"]["check"]


class FrontFix(DonFixture):
    def test_bare_front_becomes_a_question_and_stays_one(self):
        self.run_ingest()

        def judge(kind, front, back, note):
            if kind == "recall":
                return {"verdict": K["wrong"], "reason": "front is only a name", "corrected": "", "evidence": "",
                        "corrected_front": f"State: {front}", "front_only": True}
            return {"verdict": K["right"], "reason": "fine"}
        log = []
        checks = {"local": fake_check(log, judge=judge), "claude": fake_check(log, "claude", judge=judge)}
        out, _ = self.make_cards(checks=checks)
        recall = [c for c in out["cards"] if c["kind"] == "recall"]
        self.assertTrue(recall, "front-only fixes pass, they are not held back")
        self.assertTrue(all(c["front"].startswith("State: ") for c in recall))
        self.assertTrue(all(c["check"]["verdict"] == K["verdicts"]["fixed"] for c in recall))
        ids = {c["id"] for c in recall}
        log2 = []
        out2, _ = self.make_cards(checks={"local": fake_check(log2), "claude": fake_check(log2, "claude")})
        self.assertEqual(log2, [], "unchanged cards are not checked again")
        again = [c for c in out2["cards"] if c["kind"] == "recall"]
        self.assertEqual({c["id"] for c in again}, ids)
        self.assertTrue(all(c["front"].startswith("State: ") for c in again), "the fixed front is kept")


if __name__ == "__main__":
    unittest.main(verbosity=2)
