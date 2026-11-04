"""brain.json drives brain.py's nets, cues, wording and limits (AGENTS.md
Rule 0): the shipped data reproduces the old behaviour (covered by
scripts/test_reply_guards.py and friends), and changing the data changes
it. No model is called.

    .venv\\Scripts\\python.exe scripts\\test_brain_config.py
"""
import datetime
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import brain  # noqa: E402
import brain_rules  # noqa: E402


def _strip(d):
    if isinstance(d, dict):
        return {k: _strip(v) for k, v in d.items() if not k.startswith("_")}
    if isinstance(d, list):
        return [_strip(v) for v in d]
    return d


def _write_config(test, edit) -> str:
    with open(brain_rules.DATA_DEFAULTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    edit(data)
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    test.addCleanup(os.remove, path)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        brain.configure(brain_rules.DATA_FILE)

    def tearDown(self):
        brain.configure(brain_rules.DATA_FILE)

    def reconfigure(self, edit):
        brain.configure(_write_config(self, edit))


class ShippedData(Base):
    def test_shipped_file_equals_defaults(self):
        with open(brain_rules.DATA_FILE, encoding="utf-8") as a, open(brain_rules.DATA_DEFAULTS_FILE, encoding="utf-8") as b:
            self.assertEqual(_strip(json.load(a)), _strip(json.load(b)))

    def test_every_pattern_in_the_shipped_data_compiles(self):
        with self.assertNoLogs("denisse.json_config", "WARNING"):
            brain_rules.data(brain_rules.DATA_FILE)

    def test_nothing_in_the_data_is_an_eval_prompt(self):
        # style_examples must never match a case, or the eval measures
        # recall of the example rather than her voice (brain.py's note).
        from eval.cases import CASES
        prompts = {c["prompt"].strip().lower() for c in CASES}
        said = {m["content"].strip().lower() for m in brain.CONFIG["style_examples"]["messages"]
                if m.get("role") == "user"}
        self.assertFalse(prompts & said, prompts & said)


class QuickReplies(Base):
    _am = datetime.datetime(2026, 10, 29, 9, 0)

    def test_greeting_wording_and_pattern_come_from_the_data(self):
        self.assertEqual(brain._quick_reply("hey", "", self._am), "Hey Chuen.")
        self.assertIsNone(brain._quick_reply("wotcha", "", self._am))

        def edit(d):
            q = d["quick_replies"]
            q["greeting_pattern"] = r"^\s*(?:hey|wotcha)[.!\s]*$"
            q["greeting"] = "Yo {name}."
            q["thanks"] = "No bother."
        self.reconfigure(edit)
        self.assertEqual(brain._quick_reply("wotcha", "", self._am), "Yo Chuen.")
        self.assertEqual(brain._quick_reply("thanks", "", self._am), "No bother.")

    def test_day_parts_come_from_the_data(self):
        self.assertEqual(brain._quick_reply("good morning", "", self._am), "Morning, Chuen.")
        self.reconfigure(lambda d: d["quick_replies"]["day_parts"].insert(
            0, {"until_hour": 10, "name": "Early"}))
        self.assertEqual(brain._quick_reply("good morning", "", self._am), "Early, Chuen.")


class Cues(Base):
    def test_requests_answered_before_the_model_come_from_the_data(self):
        self.assertFalse(brain._NEWS_OFF.search("enough with the bulletins"))
        self.assertFalse(brain._PLAN_STUDY.search("sort out my modules"))
        self.assertFalse(brain._should_delegate("ghostwrite my blog"))
        self.assertIsNone(brain._vault_query("hows my dissertation going"))

        def edit(d):
            c = d["cues"]
            c["news_off_pattern"] = r"\benough with the bulletins\b"
            c["plan_study_patterns"].append(r"\bsort out my modules\b")
            c["delegate_patterns"].append(r"\bghostwrite\b")
            c["vault_queries"].append({"all_of_patterns": [r"\bdissertation\b"], "query": "dissertation"})
        self.reconfigure(edit)
        self.assertTrue(brain._NEWS_OFF.search("enough with the bulletins"))
        self.assertTrue(brain._PLAN_STUDY.search("sort out my modules"))
        self.assertTrue(brain._should_delegate("ghostwrite my blog"))
        self.assertEqual(brain._vault_query("hows my dissertation going"), "dissertation")

    def test_keep_local_overrides_delegate_from_the_data(self):
        self.assertTrue(brain._should_delegate("write me a cover letter"))
        self.reconfigure(lambda d: d["cues"]["keep_local_patterns"].append(r"\bcover letter\b"))
        self.assertFalse(brain._should_delegate("write me a cover letter"))

    def test_study_brief_module_names_come_from_the_data(self):
        self.assertFalse(brain._STUDY_BRIEF.search("prep me for thermodynamics"))
        self.reconfigure(lambda d: d["cues"]["module_names"].append("thermodynamics"))
        self.assertTrue(brain._STUDY_BRIEF.search("prep me for thermodynamics"))
        self.assertFalse(brain._STUDY_BRIEF.search("prep me for the party"))

    def test_feedback_commands_come_from_the_data(self):
        b = brain.Brain.__new__(brain.Brain)
        answer, _ = b.respond([{"role": "user", "content": "/good"}])
        self.assertTrue(answer.startswith("Thanks"))
        self.reconfigure(lambda d: d["text"]["feedback_commands"].append({"command": "/meh", "reply": "Noted, meh."}))
        answer, _ = b.respond([{"role": "user", "content": "/meh"}])
        self.assertEqual(answer, "Noted, meh.")


class ReplyNets(Base):
    def test_clock_wording_comes_from_the_data(self):
        now = datetime.datetime(2026, 10, 30, 18, 48)
        self.assertTrue(brain._correct_unfetched_clock("It's currently December 2023.", now)
                        .startswith("It's Friday 30 October, 6:48 PM."))
        self.reconfigure(lambda d: d["clock"].update(real_time="Right now it is {hour}:{minute} {ampm}."))
        self.assertEqual(brain._correct_unfetched_clock("It's currently December 2023.", now),
                         "Right now it is 6:48 PM.")

    def test_weather_words_come_from_the_data(self):
        self.assertEqual(brain._strip_unfetched_weather("Pollen count: high today."), "Pollen count: high today.")

        def edit(d):
            d["weather"]["data_patterns"].append(r"pollen\s+count")
            d["weather"]["line_patterns"].append(r"pollen")
            d["weather"]["not_checked"] = "No forecast fetched."
        self.reconfigure(edit)
        self.assertEqual(brain._strip_unfetched_weather("Pollen count: high today."), "No forecast fetched.")

    def test_honesty_claims_and_replies_come_from_the_data(self):
        claim = "Done. The lights are dimmed."
        self.assertFalse(brain._claim_without_its_tool(claim, {"calculate"}))
        self.assertEqual(brain.UNCONFIRMED_REPLY, "I haven't confirmed that action, so I can't say it's done.")

        def edit(d):
            d["honesty"]["claim_needs"].append({"pattern": r"\blights?\b", "tools": ["dim_lights"]})
            d["honesty"]["unconfirmed_reply"] = "Not confirmed."
        self.reconfigure(edit)
        self.assertTrue(brain._claim_without_its_tool(claim, {"calculate"}))
        self.assertFalse(brain._claim_without_its_tool(claim, {"dim_lights"}))
        self.assertEqual(brain.UNCONFIRMED_REPLY, "Not confirmed.")

    def test_no_evidence_results_come_from_the_data(self):
        self.assertFalse(brain._confirms_nothing("Quota exhausted for today."))
        self.reconfigure(lambda d: d["evidence"]["no_evidence_prefixes"].append("Quota exhausted"))
        self.assertTrue(brain._confirms_nothing("Quota exhausted for today."))

    def test_news_wording_and_limit_come_from_the_data(self):
        result = "Today's CNA headlines: Alpha story [World]; Beta story [Asia]; Gamma story [Sport]; Delta story [Tech]"
        self.assertEqual(brain._speak_headlines(result),
                         "Top stories on CNA: Alpha story. Beta story. And Gamma story.")

        def edit(d):
            d["news"]["max_headlines"] = 2
            d["news"]["many"] = "CNA says: {heads}; lastly {last}."
        self.reconfigure(edit)
        self.assertEqual(brain._speak_headlines(result), "CNA says: Alpha story; lastly Beta story.")

    def test_task_wording_comes_from_the_data(self):
        self.assertEqual(brain._task_sentence("remove_task", "Removed: buy milk."), 'Removed "buy milk" from your list.')
        self.reconfigure(lambda d: d["tasks"].update(removed="Dropped {text}."))
        self.assertEqual(brain._task_sentence("remove_task", "Removed: buy milk."), "Dropped buy milk.")

    def test_path_ask_and_search_limits_come_from_the_data(self):
        got, hidden = brain._tidy_reply(r"It's in C:\A\cv.pdf", "find my cv")
        self.assertTrue(got.endswith("Want the full path?"))
        self.assertEqual(brain._file_query("pull up my latest cv"), "cv")

        def edit(d):
            d["paths"]["ask"] = "Need the path?"
            d["file_query"]["stop_words"].remove("latest")
        self.reconfigure(edit)
        got, _ = brain._tidy_reply(r"It's in C:\A\cv.pdf", "find my cv")
        self.assertTrue(got.endswith("Need the path?"))
        self.assertEqual(brain._PATH_ASK, "Need the path?")
        self.assertEqual(brain._file_query("pull up my latest cv"), "latest cv")

    def test_skills_generic_words_come_from_the_data(self):
        listing = "daily_briefing: How to give a quick daily briefing\nquick_maths: How to do sums"
        self.assertTrue(brain._names_a_skill("I can do maths for you.", listing))
        self.reconfigure(lambda d: d["skills"]["generic_words"].append("maths"))
        self.assertFalse(brain._names_a_skill("I can do maths for you.", listing))

    def test_second_person_grammar_comes_from_the_data(self):
        self.assertEqual(brain._second_person("Chuen is a collaborator."), "You are a collaborator.")
        # After an address lead-in only a verb cue marks him as the subject.
        self.assertEqual(brain._second_person("Hey Chuen reckons it works."), "Hey Chuen reckons it works.")
        self.reconfigure(lambda d: d["second_person"]["verb_cues"].append("reckons"))
        self.assertEqual(brain._second_person("Hey Chuen reckons it works."), "Hey you reckons it works.")


class Limits(Base):
    def test_limits_come_from_the_data_and_env_still_wins(self):
        self.assertEqual(brain.MAX_TOOL_ROUNDS, 6)
        self.reconfigure(lambda d: d["limits"].update(max_tool_rounds=3, num_ctx=4096))
        self.assertEqual((brain.MAX_TOOL_ROUNDS, brain.NUM_CTX), (3, 4096))
        os.environ["DENISSE_MAX_TOOL_ROUNDS"] = "9"
        self.addCleanup(os.environ.pop, "DENISSE_MAX_TOOL_ROUNDS", None)
        self.reconfigure(lambda d: d["limits"].update(max_tool_rounds=3))
        self.assertEqual(brain.MAX_TOOL_ROUNDS, 9)

    def test_style_examples_come_from_the_data(self):
        os.environ["DENISSE_STYLE_EXAMPLES"] = "1"
        self.addCleanup(os.environ.pop, "DENISSE_STYLE_EXAMPLES", None)
        self.assertIn("yo", [m["content"] for m in brain._style_examples()])
        self.reconfigure(lambda d: d["style_examples"].update(messages=[{"role": "user", "content": "oi"}]))
        self.assertEqual([m["content"] for m in brain._style_examples()], ["oi"])


class BadData(Base):
    def test_bad_pattern_falls_back_to_the_default_with_a_warning(self):
        with self.assertLogs("denisse.json_config", "WARNING") as logs:
            self.reconfigure(lambda d: d["cues"].update(news_off_pattern="(unclosed"))
        self.assertIn("brain.cues.news_off_pattern", "\n".join(logs.output))
        self.assertTrue(brain._NEWS_OFF.search("stop the headlines"))

    def test_malformed_list_entry_falls_back_with_a_warning(self):
        with self.assertLogs("denisse.json_config", "WARNING") as logs:
            self.reconfigure(lambda d: d["honesty"]["claim_needs"].append({"pattern": r"\bx\b"}))
        self.assertIn("brain.honesty.claim_needs", "\n".join(logs.output))
        self.assertEqual(len(brain._CLAIM_NEEDS), 5)

    def test_unreadable_file_uses_the_defaults(self):
        with self.assertLogs("denisse.json_config", "WARNING"):
            brain.configure(os.path.join(ROOT, "no-such-brain-config.json"))
        self.assertEqual(brain._quick_reply("thanks", ""), "Any time.")
        self.assertTrue(brain._BRIEFING.search("brief me"))


if __name__ == "__main__":
    unittest.main()
