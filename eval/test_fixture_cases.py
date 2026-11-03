"""The eval's fixture vault and headless study-mode path (2026-11-03).

No model: checks that the harness pieces the vault_*/study_* cases rely on
do what those cases assume, so a nightly failure means Denisse, not the rig.
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import run_eval  # noqa: E402
from eval.cases import CASES  # noqa: E402
import vault_search  # noqa: E402

BY_ID = {c["id"]: c for c in CASES}


class FixtureVault(unittest.TestCase):
    def search(self, q):
        with run_eval._fixture_vault("fixture"):
            self.assertEqual(vault_search.VAULT, str(run_eval.FIXTURE_VAULT))
            return vault_search.search_vault(q)

    def test_week_note_is_found(self):
        self.assertIn("Gibbs", self.search("signals week 4"))

    def test_tutorial_solutions_link_is_found(self):
        self.assertIn("CAB Tutorial 5 Solutions.pdf", self.search("CAB tutorial 5 solutions"))

    def test_outside_file_never_surfaces(self):
        canary = ROOT / "eval" / "fixture_outside" / "outside_canary.md"
        self.assertTrue(canary.exists())
        for q in (r"..\fixture_outside\outside_canary.md", "../fixture_outside/outside_canary.md",
                  "pelican locker code", "secret"):
            self.assertNotIn("7731", self.search(q), q)

    def test_vault_restored_after_case(self):
        before = vault_search.VAULT
        with run_eval._fixture_vault("fixture"):
            pass
        self.assertEqual(vault_search.VAULT, before)

    def test_no_fixture_key_leaves_vault_alone(self):
        before = vault_search.VAULT
        with run_eval._fixture_vault(None):
            self.assertEqual(vault_search.VAULT, before)


class StudyTurn(unittest.TestCase):
    def test_start(self):
        reply, tools = run_eval.study_turn(BY_ID["study_start"])
        self.assertEqual(tools, ["study_mode:start"])
        self.assertEqual(run_eval.check(BY_ID["study_start"], reply, tools, [reply]), [])

    def test_clock_cycles(self):
        case = BY_ID["study_timer_cycles"]
        reply, tools = run_eval.study_turn(case)
        self.assertEqual(run_eval.check(case, reply, tools, [reply]), [], reply)

    def test_stop_is_clean(self):
        case = BY_ID["study_stop_clean"]
        reply, tools = run_eval.study_turn(case)
        self.assertEqual(reply, "Study mode off.")

    def test_inactive_turn_it_off_falls_through(self):
        self.assertIsNone(run_eval.study_turn(BY_ID["study_off_not_swallowed"]))

    def test_no_live_state_left(self):
        import study_mode
        before = study_mode._CURRENT
        run_eval.study_turn(BY_ID["study_start"])
        self.assertIs(study_mode._CURRENT, before)


if __name__ == "__main__":
    unittest.main()
