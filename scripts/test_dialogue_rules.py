"""dialogue_rules.json and the general rules multistep.py and clarify.py
read it with: paraphrases and typos the old fixed phrase lists missed
(each one below was checked against the code before 2026-11-03), and the
near-misses that must still not count. Synthetic text; no model, no tools.

    .venv\\Scripts\\python.exe scripts\\test_dialogue_rules.py
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import clarify  # noqa: E402
import dialogue_rules  # noqa: E402
import multistep  # noqa: E402


class DataFile(unittest.TestCase):
    def test_every_entry_says_what_it_is_for(self):
        data = json.loads((ROOT / "dialogue_rules.json").read_text(encoding="utf-8"))
        for section in ("multistep", "clarify"):
            for key, entry in data[section].items():
                self.assertIsInstance(entry, dict, key)
                self.assertTrue(str(entry.get("why", "")).strip(), f"{section}.{key} has no 'why'")
                self.assertIn("value", entry, key)

    def test_limits_come_from_the_file(self):
        rules = dialogue_rules.get("multistep")
        self.assertEqual(multistep.MAX_STEPS, rules["max_steps"])
        self.assertEqual(multistep.FILE_CHARS, rules["file_chars"])
        self.assertEqual(clarify.EXPIRY_SECONDS, dialogue_rules.get("clarify")["expiry_seconds"])

    def test_no_phrase_lists_left_in_the_code(self):
        for name in ("multistep.py", "clarify.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            for gone in ('"remind", "add"', "yes|yeah", "nope|nah", '"second": 1', "can\\s+you"):
                self.assertNotIn(gone, src, f"{name} still has {gone!r}")

    def test_closest(self):
        c = dialogue_rules.closest
        self.assertEqual(c("remnd", ["remind", "find"], 0.8, 4), "remind")
        self.assertEqual(c("yesss", ["yes"], 0.8, 4), "yes")
        self.assertEqual(c("noo", ["no"], 0.8, 4), "no")
        self.assertIsNone(c("now", ["no"], 0.8, 4))   # short words: no guessing
        self.assertIsNone(c("banana", ["remind"], 0.8, 4))


class MultistepParaphrases(unittest.TestCase):
    """Each was a single turn before (False); each is two or more jobs."""

    def test_typos_in_the_verbs(self):
        text = "fnd my signals coursework, make a revison list from it and remnd me on Thursday"
        self.assertTrue(multistep.is_multistep(text))
        self.assertEqual(len(multistep.action_parts(text)), 3)

    def test_verbs_not_on_any_list(self):
        for text in ["grab my signals coursework and ping me on thursday about it",
                     "chuck my lecture pdfs into the module folders and tell me whats missing"]:
            self.assertTrue(multistep.is_multistep(text), text)

    def test_phrasal_verb(self):
        text = "list my tasks then kick off a 25 minute timer"
        self.assertTrue(multistep.is_multistep(text))
        self.assertEqual(multistep.action_parts(text), ["list my tasks", "kick off a 25 minute timer"])

    def test_questions_are_still_not_actions(self):
        for text in ["what is the weather and is it going to rain",
                     "is my signals coursework done and what tasks are left",
                     "how do i add a task and set a timer"]:
            self.assertFalse(multistep.is_multistep(text), text)

    def test_details_keep_I_out_and_names_in(self):
        self.assertEqual(multistep.details("remind me I'll see Ben in London on Friday"),
                         ["Friday", "Ben", "London"])


def _options():
    seen = [{"label": f"{w} lab session", "text": f"{w} lab session: {d} November.", "title": "Labs"}
            for w, d in (("Electronics", 4), ("Optics", 11), ("Robotics", 25))]
    return {"options": seen[1:], "seen": seen}, {"options": seen[1:2], "seen": seen[:2]}


class ClarifyParaphrases(unittest.TestCase):
    """Each of these was 'unrelated' before: the open question was dropped."""

    def setUp(self):
        self.many, self.one = _options()

    def test_yes_however_he_says_it(self):
        for text in ["yesss", "corect", "yeah thats it thanks", "yup, that one please", "spot on"]:
            kind, got = clarify.pick(text, self.one)
            self.assertEqual(kind, "pick", text)
            self.assertIs(got, self.one["options"][0], text)
            self.assertEqual(clarify.pick(text, self.many)[0], "ambiguous", text)

    def test_no_however_he_says_it(self):
        for text in ["nah neither of them", "noo", "nope, none of those"]:
            self.assertEqual(clarify.pick(text, self.many), ("none", None), text)

    def test_misspelt_position(self):
        kind, got = clarify.pick("the secnd one", self.many)
        self.assertEqual(kind, "pick")
        self.assertIs(got, self.many["options"][1])

    def test_another(self):
        self.assertEqual(clarify.pick("another", self.one), ("pick", self.one["options"][0]))

    def test_new_requests_are_still_new(self):
        for text in ["now", "thanks", "whats the time", "whats 2 plus 2", "remind me at 3"]:
            self.assertEqual(clarify.pick(text, self.many)[0], "unrelated", text)

    def test_unknown_choice_is_still_a_choice(self):
        for text in ["the blue one", "the last one", "both"]:
            self.assertEqual(clarify.pick(text, self.many)[0], "unsupported", text)


if __name__ == "__main__":
    unittest.main()
