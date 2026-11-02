"""
The look_at_this reflex cases in eval/cases.py (2026-11-02).

The model-side runs are in docs/look_at_this_eval.md; this only keeps the
cases themselves honest without Ollama: all eight are there, each carries a
fake desktop (so a nightly run never reads the real clipboard), the four
right-use cases get the tool offered, and the four wrong-use cases really
are tempting -- each has a real selection a misfire would read.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tool_router  # noqa: E402
from eval.cases import CASES  # noqa: E402

WRONG = ["look_not_thats_great", "look_not_this_is_fine",
         "look_not_do_that_again", "look_not_here"]
RIGHT = ["look_rewrite_this", "look_translate_selected",
         "look_summarise_copied", "look_what_does_that_mean"]
BY_ID = {c["id"]: c for c in CASES}


def offered(case) -> set:
    defs = [{"function": {"name": n}} for n in ("look_at_this", "get_weather", "calculate")]
    msgs = list(case.get("before", [])) + [{"role": "user", "content": case["prompt"]}]
    with patch.dict(os.environ, {"DENISSE_TOOL_ROUTER": "1"}):
        return {d["function"]["name"] for d in tool_router.select(defs, msgs)}


class LookAtThisCases(unittest.TestCase):
    def test_all_eight_present_with_a_fake_desktop(self):
        for cid in WRONG + RIGHT:
            self.assertIn(cid, BY_ID, cid)
            desk = BY_ID[cid].get("desktop") or {}
            self.assertTrue(desk.get("selection") or desk.get("clipboard"), cid)

    def test_wrong_use_cases_forbid_the_tool(self):
        for cid in WRONG:
            self.assertIn("look_at_this", BY_ID[cid]["expect"]["none_of"], cid)

    def test_right_use_cases_need_it_and_get_it_offered(self):
        for cid in RIGHT:
            self.assertEqual(BY_ID[cid]["expect"]["any_tool_of"], {"look_at_this"}, cid)
            self.assertIn("look_at_this", offered(BY_ID[cid]), cid)


if __name__ == "__main__":
    unittest.main()
