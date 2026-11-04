"""learning.json and instincts.json drive the rules: change the data, change
the behaviour (AGENTS.md Rule 0). The default behaviour itself is covered by
eval/test_learning.py, scripts/test_feedback.py and the instincts tests.

    .venv\\Scripts\\python.exe scripts\\test_learning_config.py
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import brain_rules  # noqa: E402
import instincts  # noqa: E402
import learning  # noqa: E402


def _strip(d):
    if isinstance(d, dict):
        return {k: _strip(v) for k, v in d.items() if not k.startswith("_")}
    return d


def _write_config(test, source, edit) -> str:
    with open(source, encoding="utf-8") as f:
        data = json.load(f)
    edit(data)
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    test.addCleanup(lambda: (os.remove(path), os.rmdir(folder)))
    return path


def _turn(reply="", tools=(), results=()):
    return {"user": "x", "tools": list(tools), "results": list(results), "reply": reply}


class Shipped(unittest.TestCase):
    def test_shipped_files_equal_defaults(self):
        for mod in (learning, instincts):
            with open(mod.CONFIG_FILE, encoding="utf-8") as a, open(mod.DEFAULTS_FILE, encoding="utf-8") as b:
                self.assertEqual(_strip(json.load(a)), _strip(json.load(b)), mod.__name__)


class LearningData(unittest.TestCase):
    def tearDown(self):
        learning.configure(learning.CONFIG_FILE)

    def test_correction_and_praise_words_come_from_the_data(self):
        self.assertEqual(learning.label(_turn("Here it is."), "meh that was off")[0], "unknown")
        self.assertEqual(learning.label(_turn("Here it is."), "sweet as")[0], "unknown")

        def edit(d):
            d["correction"]["phrases"].append(r"meh\b")
            d["praise"]["phrases"].append("sweet as")
        learning.configure(_write_config(self, learning.CONFIG_FILE, edit))
        self.assertEqual(learning.label(_turn("Here it is."), "meh that was off"), ("bad", "corrected"))
        self.assertEqual(learning.label(_turn("Here it is."), "sweet as"), ("good", "thanked"))

    def test_lead_ins_come_from_the_data(self):
        self.assertFalse(learning._CORRECTION.search("mate i meant the timer"))
        learning.configure(_write_config(self, learning.CONFIG_FILE,
                                         lambda d: d["correction"]["lead_ins"].append("mate")))
        self.assertTrue(learning._CORRECTION.search("mate i meant the timer"))

    def test_tool_failure_admission_and_done_claims_come_from_the_data(self):
        self.assertFalse(learning.tool_failed("Quota exceeded for today"))
        self.assertFalse(learning.CLAIMS_DONE.search("Lights dimmed."))
        failed = _turn("Oops, quota is gone.", ["web"], ["Quota exceeded for today"])

        def edit(d):
            d["tool_failure"]["starts_with"].append(r"quota exceeded\b")
            d["claims_done"]["patterns"].append(r"\blights dimmed\b")
        learning.configure(_write_config(self, learning.CONFIG_FILE, edit))
        self.assertTrue(learning.tool_failed("Quota exceeded for today"))
        self.assertTrue(learning.CLAIMS_DONE.search("Lights dimmed."))
        self.assertEqual(learning.label(failed, None)[0], "bad")
        learning.configure(_write_config(self, learning.CONFIG_FILE, lambda d: (
            edit(d), d["admits_failure"]["words"].append("oops"))))
        self.assertEqual(learning.label(failed, None)[0], "unknown")

    def test_report_sizes_and_store_path_come_from_the_data(self):
        result = {"turns": 1, "counts": {"good": 0, "bad": 1, "unknown": 0}, "good": [],
                  "failures": [{"prompt": "y" * 200, "count": 1, "reason": "corrected", "tools": []}]}
        self.assertIn("y" * 80 + "`", "\n".join(learning.summary(result)))

        def edit(d):
            d["report"]["prompt_chars"] = 10
            d["learned_file"] = "memory/elsewhere.json"
        learning.configure(_write_config(self, learning.CONFIG_FILE, edit))
        self.assertIn("`" + "y" * 10 + "`", "\n".join(learning.summary(result)))
        self.assertEqual(learning.LEARNED_FILE, os.path.join(learning.BASE_DIR, "memory", "elsewhere.json"))

    def test_bad_pattern_falls_back_to_the_defaults_with_a_warning(self):
        with self.assertLogs("denisse.json_config", "WARNING") as logs:
            learning.configure(_write_config(self, learning.CONFIG_FILE,
                                             lambda d: d["correction"]["phrases"].append("(unclosed")))
        self.assertIn("learning.correction", "\n".join(logs.output))
        self.assertTrue(learning._CORRECTION.search("no i meant the timer"))

    def test_unreadable_file_uses_the_defaults(self):
        with self.assertLogs("denisse.json_config", "WARNING"):
            learning.configure(os.path.join(ROOT, "no-such-learning-config.json"))
        self.assertTrue(learning._THANKS.search("thanks"))
        self.assertTrue(learning.tool_failed("Error: nope"))


class InstinctsData(unittest.TestCase):
    def tearDown(self):
        instincts.configure(instincts.CONFIG_FILE)

    def test_keywords_and_fillers_come_from_the_data(self):
        self.assertEqual(instincts.keywords("please start the timer"), {"start", "timer"})
        self.assertEqual(instincts.phrase("hey please start the timer"), "start the timer")

        def edit(d):
            d["keywords"]["stop_words"].append("start")
            d["keywords"]["min_chars"] = 6
            d["phrase_fillers"]["words"].append("the")
        instincts.configure(_write_config(self, instincts.CONFIG_FILE, edit))
        self.assertEqual(instincts.keywords("please start the timer kitchen"), {"kitchen"})
        self.assertEqual(instincts.phrase("hey please start the timer"), "start timer")

    def test_no_evidence_results_come_from_the_data(self):
        turn = {"tools": ["lights"], "results": ["Lights left as they were."], "label": "good"}
        self.assertTrue(instincts._worked(turn))
        instincts.configure(_write_config(self, instincts.CONFIG_FILE,
                                          lambda d: d["results"]["declined_contains"].append("left as they were")))
        self.assertFalse(instincts._worked(turn))

    def test_activation_ratio_comes_from_the_data(self):
        need = brain_rules.load().learning_confirmations
        lesson = {"validated": True, "evidence_ids": [f"p{i}" for i in range(4 * need)],
                  "negative_ids": [f"n{i}" for i in range(need)]}
        self.assertTrue(instincts._validated(lesson))  # exactly 80% positive
        instincts.configure(_write_config(self, instincts.CONFIG_FILE,
                                          lambda d: d["activation"].update(min_positives_per_negative=5)))
        self.assertFalse(instincts._validated(lesson))

    def test_md_wording_and_feedback_command_come_from_the_data(self):
        self.assertTrue(instincts._is_feedback_command(" /bad "))

        def edit(d):
            d["wording"]["none"] = "Nothing learned."
            d["wording"]["intro"] = "Needs {confirmations} confirmations."
            d["feedback_command"] = "/nope"
        instincts.configure(_write_config(self, instincts.CONFIG_FILE, edit))
        md = instincts._md([])
        self.assertIn("Nothing learned.", md)
        self.assertIn(f"Needs {brain_rules.load().learning_confirmations} confirmations.", md)
        self.assertTrue(instincts._is_feedback_command("/nope"))
        self.assertFalse(instincts._is_feedback_command("/bad"))

    def test_bad_values_fall_back_to_the_defaults_with_a_warning(self):
        def edit(d):
            d["keywords"]["word_pattern"] = "[unclosed"
            d["lookahead_turns"] = "two"
            d["wording"]["intro"] = "{nonsense}"
        with self.assertLogs("denisse.json_config", "WARNING") as logs:
            instincts.configure(_write_config(self, instincts.CONFIG_FILE, edit))
        text = "\n".join(logs.output)
        for key in ("keywords.word_pattern", "lookahead_turns", "wording.intro"):
            self.assertIn(key, text)
        self.assertEqual(instincts.LOOKAHEAD, 2)
        self.assertEqual(instincts.keywords("start the timer"), {"start", "timer"})


if __name__ == "__main__":
    unittest.main()
