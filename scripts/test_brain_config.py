"""brain.json drives brain.py's nets, cues, wording and limits (AGENTS.md
Rule 0): the shipped data reproduces the old behaviour (covered by
scripts/test_reply_guards.py and friends), and changing the data changes
it. No model is called.

    .venv\\Scripts\\python.exe scripts\\test_brain_config.py
"""
import datetime
import json
import os
import platform
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# A full turn reads his reply preferences and the planner's pending offer;
# keep both off the real memory folder (as scripts/test_tool_policy.py does).
os.environ.setdefault("DENISSE_REPLY_PREFERENCES",
                      os.path.join(tempfile.mkdtemp(prefix="denisse-brain-config-"), "reply_preferences.json"))

import brain  # noqa: E402
import brain_rules  # noqa: E402
import planner  # noqa: E402

planner.PENDING_FILE = os.path.join(tempfile.mkdtemp(prefix="denisse-brain-config-"), "pending_plan.json")


def _call(name, args=None):
    return {"message": {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}}


def _reply(text):
    return {"message": {"content": text}}


def _turn(prompt, responses, functions=None):
    """One turn with a scripted model and isolated tools: (reply, the tool
    results the model was shown)."""
    bot = brain.Brain.__new__(brain.Brain)
    it = iter(list(responses) + [_reply("Alright.")] * 3)
    isolated = {name: (lambda **kw: "Refused: isolated tool") for name in brain.tools.TOOL_FUNCTIONS}
    isolated.update(functions or {})
    with patch.object(brain.ollama, "chat", side_effect=lambda **kw: next(it)), \
            patch.dict(brain.tools.TOOL_FUNCTIONS, isolated, clear=True):
        answer, messages = bot.respond([{"role": "user", "content": prompt}])
    return answer, [m["content"] for m in messages if m.get("role") == "tool"]


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

    def test_malformed_tool_definition_falls_back_to_the_shipped_tools(self):
        with open(brain_rules.DATA_DEFAULTS_FILE, encoding="utf-8") as f:
            shipped = len(json.load(f)["tools"]["definitions"])
        with self.assertLogs("denisse.json_config", "WARNING") as logs:
            self.reconfigure(lambda d: d["tools"]["definitions"].append({"name": "dim_lights"}))
        self.assertIn("brain.tools.definitions", "\n".join(logs.output))
        self.assertEqual(len(brain._tool_definitions(brain.CONFIG)), shipped)


class PromptAndTools(Base):
    """The system prompt and the tool schema are brain.json prompt / tools
    (third softcode pass, 2026-11-05); the shipped data reproduces the old
    text byte for byte, and editing it changes what the model is sent."""

    def test_system_prompt_is_built_from_the_data_and_this_machine(self):
        self.assertTrue(brain.SYSTEM_PROMPT.startswith(f"You are running on {platform.system()} "))
        self.assertIn(f"(currently '{brain.MODEL}', served through Ollama)", brain.SYSTEM_PROMPT)
        self.assertIn(f"whose project folder is {ROOT}.", brain.SYSTEM_PROMPT)

        def edit(d):
            p = d["prompt"]
            p["machine_facts"] = "Machine: {system} {release}; user {user}; home {home}; project {project}; sep {sep}{path_example}"
            p["path_example_windows"] = " (Windows paths). "
            p["path_example_other"] = " (POSIX paths). "
            p["identity"] = "You are Denisse on {model}. "
            p["conduct"] = "Be brief."
        self.reconfigure(edit)
        example = " (Windows paths). " if os.name == "nt" else " (POSIX paths). "
        self.assertEqual(brain.SYSTEM_PROMPT,
                         f"Machine: {platform.system()} {platform.release()}; user "
                         f"{os.environ.get('USERNAME') or os.environ.get('USER') or 'unknown'}; "
                         f"home {os.path.expanduser('~')}; project {ROOT}; sep {os.sep}{example}"
                         f"You are Denisse on {brain.MODEL}. Be brief.")

    def test_tool_definitions_come_from_the_data_in_order(self):
        names = [d["function"]["name"] for d in brain.TOOL_DEFINITIONS]
        self.assertEqual(names[:2], ["run_shell_command", "open_application"])
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("find_files", brain._TOOL_NAMES)
        self.assertEqual(brain.TOOL_DEFINITIONS[0]["type"], "function")

        def edit(d):
            defs = d["tools"]["definitions"]
            defs[0]["description"] = "Run a thing."
            defs.insert(1, {"name": "dim_lights", "description": "Dim the lights.",
                            "parameters": {"type": "object",
                                           "properties": {"level": {"type": "number", "description": "0 to 1."}},
                                           "required": ["level"]}})
        self.reconfigure(edit)
        by_name = {d["function"]["name"]: d["function"] for d in brain.TOOL_DEFINITIONS}
        self.assertEqual(by_name["run_shell_command"]["description"], "Run a thing.")
        self.assertEqual([d["function"]["name"] for d in brain.TOOL_DEFINITIONS][:2],
                         ["run_shell_command", "dim_lights"])
        self.assertEqual(by_name["dim_lights"]["parameters"]["required"], ["level"])
        self.assertIn("dim_lights", brain._TOOL_NAMES)
        # The lookup repair reads a tool's one argument from its definition.
        self.assertEqual(brain._lookup_args("dim_lights", "low"), {"level": "low"})
        self.assertEqual(brain._lookup_args("search_vault", "gym"), {"query": "gym"})
        self.assertEqual(brain._lookup_args("recall", "gym"), {"key": "gym"})

    def test_enabled_env_leaves_a_tool_out_while_the_variable_says_off(self):
        def names():
            return [d["function"]["name"] for d in brain.TOOL_DEFINITIONS]
        for var in ("DENISSE_VAULT_SEARCH", "DENISSE_TEST_NO_SHELL"):
            self.addCleanup(lambda v=var, old=os.environ.get(var): os.environ.update({v: old}) if old is not None
                            else os.environ.pop(v, None))
        os.environ["DENISSE_VAULT_SEARCH"] = "1"
        brain.configure(brain_rules.DATA_FILE)
        self.assertIn("search_vault", names())
        os.environ["DENISSE_VAULT_SEARCH"] = "0"
        brain.configure(brain_rules.DATA_FILE)
        self.assertNotIn("search_vault", names())
        self.assertIn("run_shell_command", names())
        os.environ["DENISSE_TEST_NO_SHELL"] = "off"
        self.reconfigure(lambda d: d["tools"]["definitions"][0].update(enabled_env="DENISSE_TEST_NO_SHELL"))
        self.assertNotIn("run_shell_command", names())


class ToolCallWording(Base):
    """What a tool call gets back from the code instead of the tool is
    brain.json tool_errors; what Brain() raises at startup is startup."""

    def test_run_tool_wording_comes_from_the_data(self):
        self.assertEqual(brain._run_tool("teleport", {}, None), "Error: unknown tool 'teleport'")
        self.assertEqual(brain._run_tool("calculate", {"expression": "1"}, lambda n, a: False),
                         "User declined to run 'calculate'.")

        def edit(d):
            d["tool_errors"]["unknown_tool"] = "No tool {name}."
            d["tool_errors"]["declined"] = "He said no to {name}."
        self.reconfigure(edit)
        self.assertEqual(brain._run_tool("teleport", {}, None), "No tool teleport.")
        self.assertEqual(brain._run_tool("calculate", {"expression": "1"}, lambda n, a: False),
                         "He said no to calculate.")

    def test_refusals_in_the_tool_loop_come_from_the_data(self):
        _, seen = _turn("what's 17 times 4", [_call("teleport"), _reply("68.")])
        self.assertEqual(seen, ["Refused: there is no tool called 'teleport'. Use one of the tools you were given, "
                                "or answer directly."])
        self.reconfigure(lambda d: d["tool_errors"].update(no_such_tool="Nope: {name} isn't a tool."))
        _, seen = _turn("what's 17 times 4", [_call("teleport"), _reply("68.")])
        self.assertEqual(seen, ["Nope: teleport isn't a tool."])

    def test_shipped_wording_keeps_the_prefixes_other_modules_read(self):
        import multistep
        err = {k: v for k, v in brain.CONFIG["tool_errors"].items() if not k.startswith("_")}
        for key in ("step_repeated", "retry_repeated"):
            self.assertTrue(err[key].startswith(multistep.BANNED_PREFIX), key)
        self.assertTrue(err["declined"].startswith("User declined"))
        for key in ("missing_argument", "failed"):
            self.assertTrue(err[key].startswith("Error running '{name}':"), key)
        for key in ("already_ran", "last_round", "no_such_tool", "not_offered"):
            self.assertTrue(err[key].startswith("Refused:"), key)
        # None of them is evidence that anything happened.
        for key, text in err.items():
            self.assertTrue(brain._confirms_nothing(text.format(name="x", missing="q", error="e")), key)

    def test_startup_errors_come_from_the_data(self):
        with patch.object(brain.ollama, "list", side_effect=ConnectionError("down")):
            with self.assertRaises(RuntimeError) as cm:
                brain.Brain()
        self.assertTrue(str(cm.exception).startswith("Can't reach the local Ollama server: down."), cm.exception)
        with patch.object(brain.ollama, "list", return_value={"models": []}):
            with self.assertRaises(RuntimeError) as cm:
                brain.Brain()
        self.assertEqual(str(cm.exception), f"Model '{brain.MODEL}' isn't downloaded yet. Run: ollama pull {brain.MODEL}")
        self.reconfigure(lambda d: d["startup"].update(model_missing="Pull {model} first."))
        with patch.object(brain.ollama, "list", return_value={"models": []}):
            with self.assertRaises(RuntimeError) as cm:
                brain.Brain()
        self.assertEqual(str(cm.exception), f"Pull {brain.MODEL} first.")


class NotesWording(Base):
    def test_due_year_patterns_come_from_the_data(self):
        self.assertEqual(brain._DUE_YEAR.findall("CM coursework due Fri 14 Nov 2026 at noon"), ["2026"])
        self.assertEqual(brain._YEAR.findall("That's in 2026, I think."), ["2026"])
        self.reconfigure(lambda d: d["notes"].update(due_year_pattern=r"\bby\s+(20\d{2})\b", year_pattern=r"'(\d{2})\b"))
        self.assertEqual(brain._DUE_YEAR.findall("due Fri 14 Nov 2026; hand in by 2027"), ["2027"])
        self.assertEqual(brain._YEAR.findall("in '26 or 2026"), ["26"])

    def test_notes_appended_to_a_file_miss_come_from_the_data(self):
        self.addCleanup(lambda old=os.environ.get("DENISSE_VAULT_SEARCH"): os.environ.update(
            {"DENISSE_VAULT_SEARCH": old}) if old is not None else os.environ.pop("DENISSE_VAULT_SEARCH", None))
        os.environ["DENISSE_VAULT_SEARCH"] = "1"
        brain.configure(brain_rules.DATA_FILE)
        functions = {"find_files": lambda query, location="": f"No files matching '{query}' found in Documents.",
                     "search_vault": lambda query: "From your notes: Week 5 says the solutions are on KEATS."}
        prompt = "where are the CAB tutorial 5 solutions"
        _, seen = _turn(prompt, [_call("find_files", {"query": "CAB tutorial 5"}), _reply("They're on KEATS.")], functions)
        self.assertEqual(seen[0], "No files matching 'CAB tutorial 5' found in Documents.\n\nFrom his notes:\n"
                                  "From your notes: Week 5 says the solutions are on KEATS.")
        self.reconfigure(lambda d: d["notes"].update(appended_to_result="\n\nHis notes add:\n"))
        _, seen = _turn(prompt, [_call("find_files", {"query": "CAB tutorial 5"}), _reply("They're on KEATS.")], functions)
        self.assertIn("\n\nHis notes add:\nFrom your notes:", seen[0])

    def test_not_one_found_comes_from_the_data(self):
        import clarify
        bot = brain.Brain.__new__(brain.Brain)
        pending = {"options": [{"text": "a"}], "seen": [{"text": "a"}], "terms": "meeting", "request": "when is the meeting"}

        def settle():
            with patch.object(clarify, "pick", return_value=("unsupported", None)), \
                    patch.object(clarify, "question", return_value="Which one?"), \
                    patch.object(clarify, "offer"):
                return bot._settle_which(pending, "the blue one")
        self.assertEqual(settle(), "That isn't one I found. Which one?")
        self.reconfigure(lambda d: d["text"].update(not_one_found="Not among them. "))
        self.assertEqual(settle(), "Not among them. Which one?")


class SettingsData(Base):
    """brain_rules.py's own values -- his settings file's path, size limit,
    defaults and ranges, and the fact-saving request pattern -- are brain.json
    settings / memory, read through $DENISSE_BRAIN_DATA when it changes."""

    def data_env(self, edit):
        path = _write_config(self, edit)
        old = os.environ.get(brain_rules.DATA_ENV_VAR)
        os.environ[brain_rules.DATA_ENV_VAR] = path
        self.addCleanup(lambda: os.environ.update({brain_rules.DATA_ENV_VAR: old}) if old is not None
                        else os.environ.pop(brain_rules.DATA_ENV_VAR, None))

    def test_defaults_and_ranges_come_from_the_data(self):
        self.assertEqual(brain_rules.Rules().recent_turns, 6)
        with self.assertRaises(ValueError):
            brain_rules.validate({"recent_turns": 11})
        with self.assertRaises(ValueError):
            brain_rules.validate({"save_facts": "always"})

        def edit(d):
            d["settings"]["defaults"]["recent_turns"] = 4
            d["settings"]["ranges"]["recent_turns"] = [1, 12]
            d["settings"]["save_facts_values"].append("always")
        self.data_env(edit)
        self.assertEqual(brain_rules.Rules().recent_turns, 4)
        self.assertEqual(brain_rules.validate({}).recent_turns, 4)
        self.assertEqual(brain_rules.validate({"recent_turns": 11}).recent_turns, 11)
        self.assertEqual(brain_rules.validate({"save_facts": "always"}).save_facts, "always")

    def test_rules_file_path_and_size_limit_come_from_the_data(self):
        old = os.environ.pop("DENISSE_RULES_FILE", None)
        self.addCleanup(lambda: os.environ.update({"DENISSE_RULES_FILE": old}) if old is not None else None)
        self.assertEqual(brain_rules.rules_path(), brain_rules.Path(brain_rules.__file__).parent / "memory" / "brain_rules.json")
        fd, rules_file = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"recent_turns": 3, "vault_max_notes": 2}, f, indent=2)
        self.addCleanup(os.remove, rules_file)
        os.environ["DENISSE_RULES_FILE"] = rules_file
        self.assertEqual(brain_rules.load().recent_turns, 3)

        def edit(d):
            d["settings"]["file"] = "memory/elsewhere.json"
            d["settings"]["max_file_bytes"] = 20
        self.data_env(edit)
        del os.environ["DENISSE_RULES_FILE"]
        self.assertEqual(brain_rules.rules_path().name, "elsewhere.json")
        os.environ["DENISSE_RULES_FILE"] = rules_file
        with self.assertLogs(level="WARNING") as logs:
            rules = brain_rules.load()
        self.assertIn("too large", "\n".join(logs.output))
        self.assertEqual(rules.recent_turns, 3)  # the last valid settings stand

    def test_explicit_memory_request_pattern_comes_from_the_data(self):
        self.assertTrue(brain_rules.explicit_memory_request("remember that I like tea"))
        self.assertFalse(brain_rules.explicit_memory_request("remember what I said about tea?"))
        self.assertFalse(brain_rules.explicit_memory_request("note down that I like tea"))
        self.data_env(lambda d: d["memory"].update(explicit_request_pattern=r"^\s*note down\b"))
        self.assertTrue(brain_rules.explicit_memory_request("note down that I like tea"))
        self.assertFalse(brain_rules.explicit_memory_request("remember that I like tea"))
        self.data_env(lambda d: d["memory"].update(explicit_request_pattern=""))
        self.assertFalse(brain_rules.explicit_memory_request("remember that I like tea"))


if __name__ == "__main__":
    unittest.main()
