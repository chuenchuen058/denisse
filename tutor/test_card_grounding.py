"""Fewer held-back cards: the writer quotes the note and gives its final answer.

Fixture cards follow the failure modes of the 8-9 Nov Signals and Systems
run: procedure-only problem and method backs ("use the definition"), and
recall cards from memory rather than the note (Gauss's law in an S&S note).
A fake writer and a fake checker only; no Ollama, no Claude, no real vault.
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import cards, conf, sources
from tutor.test_don import DonFixture
from tutor.test_tutor import MODULES, SS

_C = conf.CONFIG["cards"]
_R = _C["check"]["reasons"]

NOTE = ("# Fourier transform\n\n"
        "The Fourier transform of e^{-at}u(t) is 1/(a+j\\omega) for a > 0, found by integrating "
        "e^{-(a+j\\omega)t} from 0 to infinity.\n\n"
        "A delta in time transforms to a constant in frequency.\n\n"
        "To find a periodic signal's Fourier series, find the period T and w0 = 2pi/T, then compute "
        "c_n = (1/T) times the integral over one period of f(t) e^{-jn w0 t}.\n")

GOOD_RECALL = {"kind": "recall", "front": "What does a delta in time transform to in frequency?",
               "back": "A constant in frequency.", "topic": "Fourier transform",
               "quote": "A delta in time transforms to a constant in frequency.", "answer": ""}
GAUSS = {"kind": "recall", "front": "State Gauss's law in integral form.",
         "back": "\\( \\oint_S E \\cdot dA = q_{enc}/\\epsilon_0 \\)", "topic": "Fourier transform",
         "quote": "Gauss's law states that the flux through a closed surface is q over epsilon", "answer": ""}
EXP_PROBLEM = {"kind": "problem", "front": "Find the Fourier transform of f(t) = e^{-at}u(t), a > 0.",
               "back": "Use the definition: integrate e^{-(a+j\\omega)t} from 0 to infinity.",
               "topic": "Fourier transform",
               "quote": "The Fourier transform of e^{-at}u(t) is 1/(a+j\\omega) for a > 0",
               "answer": "F(\\omega) = 1/(a+j\\omega)"}
TOPHAT_PROBLEM = {"kind": "problem", "front": "Calculate the Fourier transform of a top-hat function.",
                  "back": "1. Use the analysis equation. 2. Substitute the top hat and evaluate the integral.",
                  "topic": "Fourier transform", "quote": "", "answer": ""}
VAGUE_METHOD = {"kind": "method", "front": "A signal is periodic. How do you find its Fourier series?",
                "back": "Use the formula for the Fourier series of a periodic signal.",
                "topic": "Fourier series",
                "quote": "To find a periodic signal's Fourier series, find the period T and w0 = 2pi/T",
                "answer": ""}

WRITTEN = [GOOD_RECALL, GAUSS, EXP_PROBLEM, TOPHAT_PROBLEM, VAGUE_METHOD]
LEGACY = [{k: v for k, v in card.items() if k not in ("quote", "answer")} for card in WRITTEN]


def examiner(system, user):
    """A strict fake checker: right only if the (whole, multi-line) back
    states a result the note has; wrong with no correction otherwise."""
    results = ("constant in frequency", "1/(a+j\\omega)")
    head = user.split("NOTE:\n", 1)[0]
    out = []
    for n, front, back in re.findall(r"\[(\d+)\] \(\w+\) Front: ([^\n]*)\n    Back: (.*?)\n    Topic:", head, re.S):
        if any(r in back for r in results) and "Gauss" not in front:
            out.append({"n": int(n), "verdict": _C["check"]["right"], "reason": "matches the note"})
        else:
            out.append({"n": int(n), "verdict": _C["check"]["wrong"], "corrected": "", "evidence": "",
                        "reason": "the back gives no result the note supports"})
    return json.dumps({"checks": out})


class CardGroundingTests(DonFixture):
    def setUp(self):
        super().setUp()
        self.note(os.path.join(MODULES, SS, "Fourier fixture.md"), NOTE)
        self.src = next(s for s in sources.collect(self.paths, self.ss_module) if s.stem == "Fourier fixture")
        collect = mock.patch.object(cards.sources, "collect", return_value=[self.src])
        collect.start()
        self.addCleanup(collect.stop)

    def run_cards(self, written):
        writer = lambda system, user: json.dumps({"cards": written})
        checker = examiner
        return cards.run(self.paths, self.ss_module,
                         calls={engine: writer for engine in _C["engines"]},
                         checks={engine: checker for engine in _C["engines"]},
                         today=self.today, say=self.out)

    @staticmethod
    def reasons(out):
        return collections.Counter(card["check"]["reason"].split(":")[0] for card in out["held"])

    def test_before_legacy_replies_are_all_checked_and_mostly_held(self):
        # A reply without quote/answer (the old shape) is not screened: the old behaviour.
        out = self.run_cards(LEGACY)
        self.assertEqual(len(out["cards"]), 1)
        self.assertEqual(len(out["held"]), 4)
        self.assertEqual(self.reasons(out), {_R["no_fix"].split(":")[0]: 4})

    def test_after_ungrounded_and_answerless_are_dropped_and_answers_completed(self):
        out = self.run_cards(WRITTEN)
        self.assertEqual(out["held"], [])
        fronts = {card["front"]: card for card in out["cards"]}
        self.assertEqual(set(fronts), {GOOD_RECALL["front"], EXP_PROBLEM["front"]})
        self.assertTrue(fronts[EXP_PROBLEM["front"]]["back"].endswith(
            _C["kinds"]["problem"]["answer_label"] + EXP_PROBLEM["answer"]))
        said = self.say.getvalue()
        self.assertIn(_C["ground"]["no_quote"], said)
        self.assertIn(_C["ground"]["no_answer"], said)
        deck = cards.load(self.paths, self.ss_module)
        self.assertEqual(deck["sources"][self.src.rel]["dropped"], 3)

    def test_dropped_candidate_frees_its_slot_for_a_good_one(self):
        count = _C["kinds"]["problem"]["count"]
        bad = [dict(TOPHAT_PROBLEM, front=f"Top hat {n}?") for n in range(count)]
        out = self.run_cards(bad + [EXP_PROBLEM])
        self.assertEqual([c["front"] for c in out["cards"]], [EXP_PROBLEM["front"]])

    def test_answer_already_in_back_is_not_repeated(self):
        card = dict(EXP_PROBLEM, back="Integrate from 0 to infinity: F(\\omega) = 1/(a+j\\omega).")
        out = self.run_cards([card])
        self.assertEqual(out["cards"][0]["back"], card["back"])

    def test_ungrounded_kinds_are_not_screened(self):
        # tutorial problems are new by design: their quote is not required to be in the note
        kind = next(k for k, spec in _C["kinds"].items() if not spec.get("ground") and spec["count"] > 0)
        card = {"kind": kind, "front": "A new set-up: find the transform of e^{-2t}u(t).",
                "back": "F(\\omega) = 1/(a+j\\omega) with a = 2.", "topic": "Fourier transform",
                "quote": "not in the note at all", "answer": ""}
        written = cards.generate(self.ss_module, self.src,
                                 calls={e: (lambda s, u: json.dumps({"cards": [card]})) for e in _C["engines"]},
                                 only={kind})
        self.assertEqual([c["front"] for c in written], [card["front"]])

    def test_prompt_asks_for_quote_and_answer(self):
        seen = []
        writer = lambda system, user: seen.append(user) or json.dumps({"cards": []})
        cards.generate(self.ss_module, self.src, calls={e: writer for e in _C["engines"]})
        schema = json.loads(_C["schema"])["cards"][0]
        self.assertIn("quote", schema)
        self.assertIn("answer", schema)
        self.assertTrue(all(_C["schema"] in user for user in seen))


if __name__ == "__main__":
    unittest.main()
