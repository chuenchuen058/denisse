"""The chuen:* cases (2026-11-04) and the harness checks they added.

No model: checks the rig, so a failing chuen_* case in a real run means
Denisse, not a broken expectation or a leak between cases.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import run_eval  # noqa: E402
from eval.cases import CASES  # noqa: E402

BY_ID = {c["id"]: c for c in CASES}
CHUEN = run_eval.in_group(CASES, "chuen")


def fails(expect, reply, tools=()):
    return run_eval.check({"id": "t", "expect": expect}, reply, list(tools), None)


class Suite(unittest.TestCase):
    def test_ids_unique(self):
        ids = [c["id"] for c in CASES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_area_present(self):
        areas = {c["group"].split(":", 1)[1] for c in CHUEN}
        self.assertEqual(areas, {"study", "files", "reminders", "preferences", "honesty",
                                 "vault", "chat", "safety"})
        # 29 questions plus the four follow-up turns the preferences promise.
        self.assertEqual(len(CHUEN), 33)

    def test_never_the_real_vault(self):
        for c in CHUEN:
            self.assertEqual(c.get("vault"), "fixture", c["id"])

    def test_task_complete_one_still_last(self):
        self.assertEqual(CASES[-1]["id"], "task_complete_one")

    def test_fixture_has_projects_and_deadline(self):
        import vault_search
        with run_eval._fixture_vault("fixture"):
            self.assertIn("Kestrel", vault_search.search_vault("projects working on"))
            self.assertIn("24 Nov", vault_search.search_vault("signals coursework due"))

    def test_start_fast_path(self):
        case = BY_ID["chuen_study_start"]
        reply, tools = run_eval.study_turn(case)
        self.assertEqual(run_eval.check(case, reply, tools, [reply]), [])


class NewChecks(unittest.TestCase):
    def test_max_words(self):
        self.assertEqual(fails({"max_words": 3}, "one two three"), [])
        self.assertTrue(fails({"max_words": 3}, "one two three four"))

    def test_contains_each(self):
        e = {"contains_each": [["can't"], ["undo", "permanent"]]}
        self.assertEqual(fails(e, "I can't -- that's permanent."), [])
        self.assertTrue(fails(e, "I can't do that."))

    def test_in_order(self):
        e = {"in_order": ["find_files", "open_application"]}
        self.assertEqual(fails(e, "Opened it.", ["find_files", "open_application"]), [])
        self.assertTrue(fails(e, "Opened it.", ["open_application", "find_files"]))
        self.assertTrue(fails(e, "Opened it.", ["open_application"]))

    def test_any_of(self):
        e = {"any_of": [{"any_tool_of": {"open_application"}},
                        {"contains_any": ["?"], "max_words": 5}]}
        self.assertEqual(fails(e, "Opened it.", ["open_application"]), [])
        self.assertEqual(fails(e, "Which module?"), [])
        self.assertTrue(fails(e, "Here is a long answer that asks nothing at all."))

    def test_deny_still_applies_with_any_of(self):
        e = {"any_of": [{}], "deny": ["moved"]}
        self.assertTrue(fails(e, "I moved them."))


class Isolation(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="denisse-chuen-test-"))

    def test_preferences_seeded_and_restored(self):
        import reply_preferences as rp
        f = self.dir / "prefs.json"
        f.write_text(json.dumps([{"text": "his real one", "at": "x"}]), encoding="utf-8")
        with patch.object(rp, "FILE", str(f)):
            after = {}
            with run_eval._case_preferences(["Keep answers short"], after):
                self.assertEqual([p["text"] for p in rp.load()], ["Keep answers short"])
                rp.add("say me not Chuen")
            self.assertTrue(after["preference_added"])
            self.assertEqual([p["text"] for p in rp.load()], ["his real one"])
            after = {}
            with run_eval._case_preferences(None, after):
                self.assertEqual(rp.load(), [])
            self.assertFalse(after["preference_added"])

    def test_tasks_own_store_and_restored(self):
        import tasks
        f = self.dir / "tasks.json"
        with patch.object(tasks, "TASKS_FILE", str(f)), patch.object(tasks, "_mirror_to_vault", lambda: None):
            tasks.add_task("robotics meeting", "")
            tasks.add_task("unrelated thing", "")
            before = f.read_text(encoding="utf-8")
            after = {}
            with run_eval._case_tasks([["robotics meeting", "today 11pm"]], after):
                texts = [t["text"] for t in tasks.list_tasks()]
                self.assertEqual(sorted(texts), ["file the Spritzer paperwork", "robotics meeting"])
                tasks.remove_task("robotics")
            self.assertEqual(after["open_tasks"], ["file the spritzer paperwork"])
            self.assertEqual(f.read_text(encoding="utf-8"), before)

    def test_tasks_key_absent_leaves_store(self):
        after = {}
        with run_eval._case_tasks(None, after):
            pass
        self.assertNotIn("open_tasks", after)


if __name__ == "__main__":
    unittest.main()
