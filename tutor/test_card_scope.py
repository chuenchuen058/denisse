"""Module boundaries for cards: temporary vaults and fake writers/checkers only."""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import cards, conf, don, sources, topics
from tutor.test_don import DonFixture, fake_check
from tutor.test_tutor import MODULES, SS


GAUSS = {"kind": "recall", "front": "State Gauss's law in integral form.",
         "back": "The electric flux through a closed surface is Q_enc / epsilon_0.",
         "topic": "Electromagnetism"}
FOURIER = {"kind": "recall", "front": "How is the Fourier transform used to find an LTI system's output?",
           "back": "Multiply the input spectrum by the frequency response: Y(w) = H(w)X(w).",
           "topic": "Fourier transform"}
HUB = ("# Signals and Systems\n\nThis module studies signals and linear time-invariant systems.\n\n"
       "## Related modules\n- [[Electromagnetism notes]] (shares the vector/field maths).\n")


class CardScopeTests(DonFixture):
    def setUp(self):
        super().setUp()
        self.em = next(m for m in conf.modules(self.paths) if m.title == "Electromagnetism")
        self.hub_rel = os.path.join(MODULES, SS, "Signals and Systems notes.md")
        self.note(self.hub_rel, HUB)
        self.maps(["Fourier transform"], ["Gauss's law", "Fourier transform"])
        # Keep this fixture focused on one source; source discovery itself is real.
        self.src = next(s for s in sources.collect(self.paths, self.ss_module)
                        if s.stem == "Signals and Systems notes")
        collect = mock.patch.object(cards.sources, "collect", return_value=[self.src])
        collect.start()
        self.addCleanup(collect.stop)
        for patcher in (mock.patch.object(conf, "paths", side_effect=AssertionError("real paths forbidden")),
                        mock.patch("tutor.anki.Connect", side_effect=AssertionError("real Anki forbidden"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def maps(self, own, other):
        for module, names in ((self.ss_module, own), (self.em, other)):
            topics.save(self.paths, module, {"module": module.title, "code": module.code,
                        "topics": [{"name": n} for n in names]})

    def make(self, candidates, judge=None, writer_log=None, check_log=None):
        writer_log = [] if writer_log is None else writer_log
        check_log = [] if check_log is None else check_log

        def writer(system, user):
            writer_log.append((system, user))
            return json.dumps({"cards": candidates})

        def checker(system, user):
            check_log.append((system, user))
            return fake_check([], judge=judge)(system, user)

        return cards.run(self.paths, self.ss_module,
                         calls={engine: writer for engine in conf.CONFIG["cards"]["engines"]},
                         checks={engine: checker for engine in conf.CONFIG["cards"]["engines"]},
                         today=self.today, say=self.out)

    def test_gauss_from_hub_link_is_held_even_if_checker_would_approve(self):
        out = self.make([GAUSS])
        self.assertEqual(out["cards"], [])
        self.assertEqual(len(out["held"]), 1)
        self.assertEqual(out["held"][0]["check"]["reason"], "other module")
        deck = cards.load(self.paths, self.ss_module)
        self.assertEqual(cards.checked_cards(deck), [])
        note = self.read(don.cards_path(self.paths, self.ss_module))
        self.assertIn("other module", note.split(conf.CONFIG["don"]["cards_held_heading"])[1])

    def test_shared_fourier_taught_inside_current_module_is_kept(self):
        self.src.text += "\nThe Fourier transform gives the LTI output spectrum Y(w) = H(w)X(w).\n"
        out = self.make([FOURIER, GAUSS])
        self.assertEqual([c["front"] for c in out["cards"]], [FOURIER["front"]])
        self.assertEqual([c["front"] for c in out["held"]], [GAUSS["front"]])

    def test_generator_marks_foreign_topic_before_any_checker_runs(self):
        scope = cards.module_scope(self.paths, self.ss_module, [self.src])
        generated = cards.generate(self.ss_module, self.src, only={"recall"}, scope=scope,
                                   calls={"local": lambda *_: json.dumps({"cards": [GAUSS]})})
        self.assertEqual(generated[0]["check"]["verdict"], conf.CONFIG["cards"]["check"]["verdicts"]["wrong"])
        self.assertEqual(generated[0]["check"]["reason"], "other module")

    def test_checker_enforces_map_for_preexisting_candidates(self):
        scope = cards.module_scope(self.paths, self.ss_module, [self.src])
        for topic in ("Electromagnetism", "  gAuSs'S   LAW  "):
            with self.subTest(topic=topic):
                checker = mock.Mock(side_effect=AssertionError("known foreign topic needs no model"))
                passed, held, _ = cards.check(self.ss_module, self.src, [{**GAUSS, "topic": topic}], {},
                                              checks={"local": checker}, scope=scope)
                self.assertEqual(passed, [])
                self.assertEqual(held[0]["check"]["reason"], "other module")
                checker.assert_not_called()

    def test_semantic_rejection_catches_relabelled_card_and_cannot_be_corrected(self):
        # Deliberately use an own-module topic; only the checker can spot the mismatch.
        candidate = {**GAUSS, "topic": FOURIER["topic"]}
        k = conf.CONFIG["cards"]["check"]
        for verdict, reason in ((k["other_module"], "This tests a different subject."),
                                (k["wrong"], k["reasons"]["other_module"])):
            with self.subTest(verdict=verdict):
                answer = {"verdict": verdict, "reason": reason, "corrected_front": "State the electric flux law.",
                          "front_only": True, "corrected": "Still a foreign fact.", "evidence": HUB}
                passed, held, _ = cards.check(self.ss_module, self.src, [candidate.copy()], {},
                                              checks={"local": fake_check([], judge=lambda *_: answer)})
                self.assertEqual(passed, [])
                self.assertEqual(held[0]["check"]["reason"], "other module")
                self.assertEqual(held[0]["front"], candidate["front"])
                self.assertEqual(held[0]["back"], candidate["back"])

    def test_plain_cross_module_mention_without_maps_is_judged(self):
        for module in (self.ss_module, self.em):
            os.remove(self.paths.topics_file(module.slug))
        self.src.text = HUB.replace("[[Electromagnetism notes]]", "Electromagnetism")
        candidate = {**GAUSS, "topic": "Closed-surface flux"}
        check_log = []
        out = self.make([candidate], judge=lambda *_: {"verdict": "other module", "reason": "only mentioned"},
                        check_log=check_log)
        self.assertEqual(out["cards"], [])
        self.assertEqual(out["held"][0]["check"]["reason"], "other module")
        self.assertEqual(len(check_log), 1)
        self.assertIn(self.src.text, check_log[0][1])

    def test_own_note_topic_supplies_shared_topic_missing_from_map(self):
        self.maps([], [FOURIER["topic"]])
        self.src.topic = FOURIER["topic"]
        self.src.text = "# Fourier transform\n\n" + FOURIER["front"] + "\n" + FOURIER["back"]
        out = self.make([FOURIER])
        self.assertEqual([c["front"] for c in out["cards"]], [FOURIER["front"]])
        self.assertEqual(out["held"], [])

    def test_both_prompts_get_maps_note_topic_and_configured_rule(self):
        self.src.topic = "Frequency response"
        writer_log, check_log = [], []
        out = self.make([FOURIER], writer_log=writer_log, check_log=check_log)
        self.assertEqual(len(out["cards"]), 1)
        for system, user in writer_log + check_log:
            self.assertIn(conf.CONFIG["cards"]["scope"]["rule"].format(module=self.ss_module.title), system)
            for name in ("Fourier transform", "Frequency response", "Gauss's law", self.em.title):
                self.assertIn(name, user.split("NOTE:\n")[0])
            self.assertIn(self.src.text, user)
            self.assertNotIn("State Gauss", user)
        self.assertIn("Topic: Fourier transform", check_log[0][1])

    def test_unchanged_run_reuses_check_but_changed_topic_ownership_holds_card(self):
        self.make([FOURIER])
        writer_log, check_log = [], []
        self.make([FOURIER], writer_log=writer_log, check_log=check_log)
        self.assertEqual((writer_log, check_log), ([], []))
        self.maps([], [FOURIER["topic"]])
        out = self.make([FOURIER], writer_log=writer_log, check_log=check_log)
        self.assertEqual(out["cards"], [])
        self.assertEqual(out["held"][0]["check"]["reason"], "other module")
        self.assertEqual(writer_log, [], "map changes do not need to rewrite the cards")
        self.maps([FOURIER["topic"]], [FOURIER["topic"]])
        out = self.make([FOURIER], check_log=check_log)
        self.assertEqual(len(out["cards"]), 1, "a previously held topic can become shared")
        self.assertTrue(check_log, "new ownership needs a fresh semantic check")

    def test_legacy_approval_is_rechecked_without_regenerating(self):
        self.make([FOURIER])
        deck = cards.load(self.paths, self.ss_module)
        for src in deck["sources"].values():
            for card in src["cards"]:
                card["check"].pop("scope")
        don.save_json(self.paths, cards.sidecar(self.ss_module), deck)
        writer_log, check_log = [], []
        out = self.make([FOURIER], judge=lambda *_: {"verdict": "other module"},
                        writer_log=writer_log, check_log=check_log)
        self.assertEqual(writer_log, [])
        self.assertTrue(check_log)
        self.assertEqual(out["cards"], [])
        self.assertEqual(out["held"][0]["check"]["reason"], "other module")

    def test_rule_change_invalidates_check_and_wording_is_configured(self):
        self.make([FOURIER])
        policy = conf.CONFIG["cards"]["scope"]
        k = conf.CONFIG["cards"]["check"]
        writer_log, check_log = [], []
        with mock.patch.dict(policy, {"rule": policy["rule"] + "\nOnly this module's applications."}), \
                mock.patch.dict(k, {"other_module": "outside syllabus"}), \
                mock.patch.dict(k["reasons"], {"other_module": "outside syllabus"}):
            out = self.make([FOURIER], judge=lambda *_: {"verdict": "outside syllabus"},
                            writer_log=writer_log, check_log=check_log)
        self.assertEqual(writer_log, [])
        self.assertTrue(check_log)
        self.assertEqual(out["cards"], [])
        self.assertEqual(out["held"][0]["check"]["reason"], "outside syllabus")

    def test_topic_label_is_part_of_check_identity(self):
        first = copy.deepcopy(FOURIER)
        self.assertNotEqual(cards.check_key(first), cards.check_key({**first, "topic": "Electromagnetism"}))

    def test_changed_source_cannot_reuse_approval_after_teaching_is_removed(self):
        self.src.text = HUB + "\n" + FOURIER["front"] + "\n" + FOURIER["back"]
        self.make([FOURIER])
        self.src.text = HUB
        check_log = []
        out = self.make([FOURIER], judge=lambda *_: {"verdict": "other module"}, check_log=check_log)
        self.assertTrue(check_log, "a previous pass cannot stand when its source evidence changed")
        self.assertEqual(out["cards"], [])
        self.assertEqual(out["held"][0]["check"]["reason"], "other module")


if __name__ == "__main__":
    unittest.main()
