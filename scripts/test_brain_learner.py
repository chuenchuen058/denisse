"""Denisse's system context quotes the learner model (learner/core.py): the
areas, cap and wording come from brain.json's "learner" section, the store is
read on every turn, and a missing store changes nothing. A temp learner store
and a scripted model; no Ollama, no vault.

    .venv\\Scripts\\python.exe scripts\\test_brain_learner.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="denisse-brain-learner-")
os.environ.setdefault("DENISSE_REPLY_PREFERENCES", os.path.join(_TMP, "reply_preferences.json"))

import brain  # noqa: E402
import brain_rules  # noqa: E402
import planner  # noqa: E402
from learner import core  # noqa: E402

planner.PENDING_FILE = os.path.join(_TMP, "pending_plan.json")


def _belief(i, area, claim, status="current"):
    return {"id": f"b{i}", "claim": claim, "area": area, "source": "stated",
            "evidence": "", "question": None, "date": "2026-11-09T12:00:00", "status": status}


class LearnerContext(unittest.TestCase):
    def setUp(self):
        brain.configure(brain_rules.DATA_FILE)
        self.lc = brain.CONFIG["learner"]
        self.dir = tempfile.mkdtemp(prefix="learner-store-")
        self.store = Path(self.dir) / "model.json"
        p = patch.object(core, "STORE", self.store)
        p.start()
        self.addCleanup(p.stop)
        brain._LEARNER_WARNED = False

    def tearDown(self):
        brain.configure(brain_rules.DATA_FILE)

    def write(self, beliefs):
        self.store.write_text(json.dumps({"beliefs": beliefs, "questions": []}), encoding="utf-8")

    def system_seen(self, prompt="plan my week"):
        """One turn with a scripted model: the system message it was given."""
        seen = []

        def chat(**kw):
            seen.append(kw["messages"][0]["content"])
            return {"message": {"content": "Alright."}}
        bot = brain.Brain.__new__(brain.Brain)
        isolated = {name: (lambda **kw: "Refused: isolated tool") for name in brain.tools.TOOL_FUNCTIONS}
        with patch.object(brain.ollama, "chat", side_effect=chat), \
                patch.dict(brain.tools.TOOL_FUNCTIONS, isolated, clear=True):
            bot.respond([{"role": "user", "content": prompt}])
        self.assertTrue(seen, "the model was never called")
        return seen[0]

    def test_config_shape_and_defaults_equal(self):
        with open(brain_rules.DATA_DEFAULTS_FILE, encoding="utf-8") as f:
            defaults = json.load(f)["learner"]
        with open(brain_rules.DATA_FILE, encoding="utf-8") as f:
            live = json.load(f)["learner"]
        self.assertEqual(defaults, live)
        for key in ("areas", "max", "line", "none", "block"):
            self.assertIn(key, self.lc)
        self.assertTrue(set(self.lc["areas"]) <= set(core.AREAS))

    def test_claims_appear_in_the_context(self):
        area = self.lc["areas"][0]
        self.write([_belief(1, area, "Takes feedback blunt and direct")])
        system = self.system_seen()
        expected = self.lc["block"].format(claims=self.lc["line"].format(claim="Takes feedback blunt and direct"))
        self.assertIn(expected, system)

    def test_read_at_request_time(self):
        area = self.lc["areas"][0]
        self.write([_belief(1, area, "first habit")])
        self.assertIn("first habit", self.system_seen())
        self.write([_belief(1, area, "first habit", status="superseded"), _belief(2, area, "second habit")])
        system = self.system_seen()
        self.assertIn("second habit", system)
        self.assertNotIn("first habit", system)

    def test_cap_and_area_filter_hold(self):
        area = self.lc["areas"][0]
        outside = next((a for a in core.AREAS if a not in self.lc["areas"]), None)
        beliefs = [_belief(i, area, f"habit number {i:02d}") for i in range(self.lc["max"] + 5)]
        if outside:
            beliefs.append(_belief(99, outside, "an area Denisse does not quote"))
        self.write(beliefs)
        block = brain._learner_context()
        self.assertEqual(block.count("habit number"), self.lc["max"])
        self.assertNotIn("an area Denisse does not quote", block)

    def test_cap_and_areas_come_from_config(self):
        self.write([_belief(1, "goals", "g claim"), _belief(2, "feedback", "f claim"),
                    _belief(3, "study", "s claim")])
        brain.CONFIG["learner"] = dict(self.lc, areas=["study", "goals"], max=1,
                                       line="* {claim}", block="HABITS\n{claims}")
        self.assertEqual(brain._learner_context(), "HABITS\n* s claim")

    def test_missing_store_runs_as_before_and_logs_once(self):
        self.assertFalse(self.store.exists())
        with self.assertLogs(level="INFO") as logs:
            first = self.system_seen()
            second = self.system_seen()
        prefix = self.lc["missing_log"].split("{")[0].strip()
        self.assertEqual(sum(prefix in line for line in logs.output), 1)
        self.assertEqual(first, second)
        if not self.lc["none"]:
            self.assertNotIn(self.lc["block"].split("{")[0].strip(), first)

    def test_broken_store_does_not_crash(self):
        self.store.write_text("{not json", encoding="utf-8")
        self.assertEqual(brain._learner_context(), self.lc["none"])
        with patch.object(core, "load", side_effect=OSError("disk gone")):
            self.write([])
            self.assertEqual(brain._learner_context(), self.lc["none"])


if __name__ == "__main__":
    unittest.main()
