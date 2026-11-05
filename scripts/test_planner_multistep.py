"""multistep.py: the detector, the plan schema, step-by-step execution and
the one retry. Model and tools are isolated -- no Ollama, no real writes."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain  # noqa: E402
import multistep  # noqa: E402
import time_parse  # noqa: E402


# The fixtures say "Thursday". On a Thursday time_parse rightly asks "today or next Thursday?", so the
# add_task step never ran and 3 tests failed every Thursday (5 Nov 2026: seven Orchestra tasks parked on it).
# Pin the clock to a Monday so the tests don't depend on what day they run.
def setUpModule():
    from datetime import datetime
    time_parse.set_clock(datetime(2026, 11, 2, 9, 0))


def tearDownModule():
    time_parse.set_clock(None)

SIGNALS = "find my signals coursework, make a revision list from it and remind me on Thursday"
LECTURES = "move this week's lecture PDFs into the right module folders and tell me what's missing"


def reply(text):
    return {"message": {"content": text}}


def call(name, args=None):
    return {"message": {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}}


class Script:
    """A fake ollama.chat. The plan call (format=...) gets `plan`; every
    other call is answered from the queue for its step's user message."""

    def __init__(self, plan, steps):
        self.plan = plan
        self.steps = {k: list(v) for k, v in steps.items()}
        self.calls = []   # (step text, offered tool names, system text)

    def __call__(self, **kw):
        if "format" in kw:
            self.calls.append(("<plan>", None, ""))
            return reply(self.plan if isinstance(self.plan, str) else json.dumps(self.plan))
        msgs = kw["messages"]
        step = next(m["content"] for m in reversed(msgs) if m["role"] == "user")
        offered = None if kw.get("tools") is None else {t["function"]["name"] for t in kw["tools"]}
        self.calls.append((step, offered, msgs[0]["content"]))
        queue = self.steps.get(step, [])
        return queue.pop(0) if queue else reply("Okay.")


def run(prompt, script, functions, confirm=None):
    bot = brain.Brain.__new__(brain.Brain)
    isolated = {n: (lambda **kw: "Refused: isolated tool") for n in brain.tools.TOOL_FUNCTIONS}
    isolated.update(functions)
    with patch.object(brain.ollama, "chat", side_effect=script), \
            patch.dict(brain.tools.TOOL_FUNCTIONS, isolated, clear=True):
        return bot.respond([{"role": "user", "content": prompt}], confirm=confirm)


class Detector(unittest.TestCase):
    def test_roadmap_examples_are_multistep(self):
        self.assertTrue(multistep.is_multistep(SIGNALS))
        self.assertTrue(multistep.is_multistep(LECTURES))
        self.assertEqual(len(multistep.action_parts(SIGNALS)), 3)

    def test_other_multistep_shapes(self):
        for text in ["check the weather in London and remind me to take an umbrella tomorrow",
                     "list my tasks and then start a 25 minute timer",
                     "can you get me the news headlines, then tell me what's on my plate"]:
            self.assertTrue(multistep.is_multistep(text), text)

    def test_single_jobs_are_not(self):
        for text in ["find and open my cv", "remind me to buy milk and eggs",
                     "add a task to call mum and dad", "what's the weather and should i bring an umbrella",
                     "pros and cons of rust", "hey", "what's on my plate?", "find my japan flight pdf",
                     "tell me a joke and make it funny",  # no tool cue anywhere
                     ""]:
            self.assertFalse(multistep.is_multistep(text), text)

    def test_a_reply_preference_is_never_a_plan(self):
        # Two verb phrases, one instruction about how she replies (2026-11-04).
        self.assertFalse(multistep.is_multistep("stop reading out file paths, just ask me if i want them"))
        self.assertFalse(multistep.is_multistep("from now on keep it short and call me boss"))

    def test_no_existing_eval_prompt_is_planned(self):
        from eval.cases import CASES
        planned = [c["id"] for c in CASES if multistep.is_multistep(c["prompt"])
                   and not c["id"].startswith("multi_")]
        self.assertEqual(planned, [])

    def test_every_multistep_eval_case_is_detected(self):
        from eval.cases import CASES
        multi = [c for c in CASES if c["id"].startswith("multi_")]
        self.assertEqual(len(multi), 6)
        for c in multi:
            self.assertTrue(multistep.is_multistep(c["prompt"]), c["id"])


class EvalHarness(unittest.TestCase):
    def test_all_tools_of(self):
        from eval.run_eval import check
        case = {"expect": {"all_tools_of": {"get_weather", "add_task"}}}
        self.assertEqual(check(case, "Rain, and I'll remind you.", ["get_weather", "add_task"]), [])
        self.assertTrue(check(case, "Rain today.", ["get_weather"]))

    def test_fixtures_replace_the_tool_for_one_case_only(self):
        from eval import run_eval
        real = brain.tools.TOOL_FUNCTIONS["get_weather"]
        seen = {}

        class Bot:
            def respond(self, messages, confirm=None):
                seen["result"] = brain.tools.TOOL_FUNCTIONS["get_weather"](location="x")
                return "Light rain.", messages
        r = run_eval.run_case(Bot(), {"id": "t", "prompt": "weather?", "expect": {},
                                      "fixtures": {"get_weather": "London: light rain"}}, [])
        self.assertEqual(seen["result"], "London: light rain")
        self.assertTrue(r["passed"], r["failures"])
        self.assertIs(brain.tools.TOOL_FUNCTIONS["get_weather"], real)


class PlanSchema(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(multistep.parse_plan('{"steps": ["Find it", "  Remind   me "]}'),
                         ["Find it", "Remind me"])

    def test_rejected(self):
        for raw in ['{"steps": ["only one"]}', json.dumps({"steps": ["s"] * 6}), "not json",
                    '{"steps": "find"}', '{"steps": ["ok", 3]}', '{"steps": ["ok", ""]}',
                    json.dumps({"steps": ["ok", "x" * 201]}), "[]", None]:
            self.assertIsNone(multistep.parse_plan(raw), raw)

    def test_checked_against_his_request(self):
        good = {"steps": ["Find my signals coursework", "Make a revision list from my signals coursework",
                          "Remind me about my signals coursework on Thursday"]}
        self.assertEqual(multistep.parse_plan(json.dumps(good), SIGNALS), good["steps"])
        self.assertIsNone(multistep.parse_plan(json.dumps({"steps": good["steps"][:2]}), SIGNALS))
        self.assertIsNone(multistep.parse_plan(json.dumps(
            {"steps": good["steps"][:2] + ["Book a dentist appointment for next week"]}), SIGNALS))

    def test_schema_caps_at_five(self):
        self.assertEqual(multistep._PLAN_SCHEMA["properties"]["steps"]["maxItems"], 5)


STEP1, STEP2, STEP3 = ("Find my signals coursework notes", "Make a revision list from it",
                       "Remind me on Thursday to revise signals")


class Execution(unittest.TestCase):
    def test_steps_run_in_order_with_their_own_tools(self):
        script = Script({"steps": [STEP1, STEP2, STEP3]}, {
            STEP1: [call("search_vault", {"query": "signals coursework"}), reply("Your signals coursework covers Fourier series and convolution.")],
            STEP2: [reply("Revision list: 1. Fourier series 2. convolution.")],
            STEP3: [call("add_task", {"text": "revise signals", "due": "Thursday"}), reply("Added.")],
        })
        added = []
        answer, msgs = run(SIGNALS, script, {
            "search_vault": lambda **kw: "From your notes: Signals coursework -- Fourier series, convolution. Due 24 Nov 2026.",
            "add_task": lambda **kw: added.append(kw) or "Added: revise signals (due Thu 5 Nov).",
        })
        self.assertEqual(script.calls[0][0], "<plan>")
        by_step = {}
        for step, offered, system in script.calls[1:]:
            by_step.setdefault(step, []).append((offered, system))
        # Each step is offered its own tools, never the whole request's.
        self.assertIn("search_vault", by_step[STEP1][0][0])
        self.assertNotIn("add_task", by_step[STEP1][0][0])
        self.assertIn("add_task", by_step[STEP3][0][0])
        self.assertNotIn("search_vault", by_step[STEP3][0][0])
        # Later steps see the earlier results, summarised.
        self.assertIn("Fourier series", by_step[STEP2][0][1])
        self.assertIn("step 2 of 3", by_step[STEP2][0][1])
        self.assertNotIn("Earlier steps", by_step[STEP1][0][1])
        # The plan note lives in that call only, never in SYSTEM_PROMPT.
        self.assertNotIn("step 1 of", brain.SYSTEM_PROMPT)
        self.assertEqual(len(added), 1)
        self.assertIn("Fourier", answer)
        self.assertIn("revise signals", answer)
        names = [c["function"]["name"] for m in msgs if m.get("tool_calls") for c in m["tool_calls"]]
        self.assertEqual(names, ["search_vault", "add_task"])
        self.assertEqual(msgs[-1], {"role": "assistant", "content": answer})

    def test_step_never_gets_a_tool_the_request_did_not_route(self):
        # "Play some music" as a step of a request that never mentioned music.
        request = [{"role": "user", "content": "list my tasks and then start a 25 minute timer"}]
        ceiling = {d["function"]["name"] for d in brain.tool_router.select(brain.TOOL_DEFINITIONS, request)}
        self.assertNotIn("play_song", multistep.step_tools("Play some music", None, ceiling))
        self.assertIn("set_timer", multistep.step_tools("start a 25 minute timer", "list my tasks", ceiling))

    def test_a_step_that_refers_back_gets_the_earlier_steps_tools(self):
        ceiling = {d["function"]["name"] for d in brain.TOOL_DEFINITIONS}
        alone = multistep.step_tools("make a revision list from it", None, ceiling)
        after = multistep.step_tools("make a revision list from it", "find my signals coursework", ceiling)
        self.assertNotIn("read_file", alone)
        self.assertIn("read_file", after)
        self.assertIn("search_vault", after)

    def test_bad_plan_falls_back_to_his_own_parts(self):
        script = Script("not a plan", {})
        run(SIGNALS, script, {})
        self.assertEqual([c[0] for c in script.calls],
                         ["<plan>", "find my signals coursework", "make a revision list from it",
                          "remind me on Thursday"])

    def test_invented_step_falls_back_to_his_own_parts(self):
        script = Script({"steps": ["Open the computer and navigate to the folder where the Japan "
                                   "flight documents are stored", "Search for the Japan flight PDF"]}, {})
        run("find my japan flight pdf and then add a task to check in on friday", script, {})
        self.assertEqual([c[0] for c in script.calls][1:],
                         ["find my japan flight pdf", "add a task to check in on friday"])

    def test_unreachable_model_answers_as_one_turn(self):
        script = Script({}, {SIGNALS: [reply("One turn.")]})

        def chat(**kw):
            if "format" in kw:
                raise ConnectionError("ollama down")
            return script(**kw)
        answer, _ = run(SIGNALS, chat, {})
        self.assertEqual(answer, "One turn.")

    def test_path_offer_moves_to_the_end(self):
        self.assertEqual(multistep._compose(["It's in Coursework.md. " + brain._PATH_ASK, "Added: revise."]),
                         "It's in Coursework.md.\n\nAdded: revise. " + brain._PATH_ASK)

    def test_switch_off(self):
        script = Script({"steps": [STEP1, STEP3]}, {SIGNALS: [reply("One turn.")]})
        with patch.dict(os.environ, {"DENISSE_PLANNER": "0"}):
            answer, _ = run(SIGNALS, script, {})
        self.assertEqual(answer, "One turn.")
        self.assertNotIn("<plan>", [c[0] for c in script.calls])

    def test_mutation_from_an_earlier_step_is_not_repeated(self):
        a, b = "Remind me on Thursday to revise signals", "Add revise signals to my tasks for Thursday"
        args = {"text": "revise signals", "due": "Thursday"}
        script = Script({"steps": [a, b]}, {a: [call("add_task", args), reply("Added.")],
                                            b: [call("add_task", args), reply("Added."), reply("Already on your list.")]})
        added = []
        run("remind me on Thursday to revise signals, then add revise signals to my tasks", script,
            {"add_task": lambda **kw: added.append(kw) or "Added: revise signals (due Thu)."})
        self.assertEqual(len(added), 1)


class Retry(unittest.TestCase):
    A, B = "Find this week's lecture PDFs", "Tell me what's missing"

    def test_error_gets_one_retry_with_that_call_banned(self):
        bad = {"query": "lecture"}
        script = Script({"steps": [self.A, self.B]}, {
            self.A: [call("find_files", bad), reply("Hmm."),            # attempt 1: error
                     call("find_files", bad), call("find_files", {"query": "week 6 pdf"}),
                     reply("Found two lecture PDFs.")],                  # attempt 2: different call
            self.B: [reply("Week 6 tutorial slides are missing.")],
        })
        seen = []

        def find(**kw):
            seen.append(kw["query"])
            return "Error running 'find_files': index busy." if kw["query"] == "lecture" else \
                "Found 2 files:\nC:\\Users\\Xichu\\Downloads\\L6.pdf\nC:\\Users\\Xichu\\Downloads\\T6.pdf"
        answer, _ = run(LECTURES, script, {"find_files": find})
        # The banned call never reached the tool a second time.
        self.assertEqual(seen, ["lecture", "week 6 pdf"])
        retry_system = [s for step, _o, s in script.calls if step == self.A][2]
        self.assertIn("last try at this step failed", retry_system)
        self.assertIn("index busy", retry_system)
        self.assertIn("missing", answer)
        self.assertNotIn("stopped", answer)

    def test_second_failure_stops_and_says_what_failed(self):
        script = Script({"steps": [self.A, self.B]}, {
            self.A: [call("find_files", {"query": "lecture"}), reply("Hmm."),
                     call("find_files", {"query": "pdf"}), reply("Hmm.")],
            self.B: [reply("should never run")],
        })
        answer, _ = run(LECTURES, script, {"find_files": lambda **kw: "Error running 'find_files': drive offline."})
        self.assertIn("I stopped at step 1", answer)
        self.assertIn("drive offline", answer)
        self.assertIn("Not done: Tell me what's missing", answer)
        self.assertNotIn(self.B, [c[0] for c in script.calls])
        # Never loops: plan + two attempts of two model calls each.
        self.assertEqual(len(script.calls), 5)

    def test_honesty_net_rejection_is_retried(self):
        a, b = "Move the lecture PDFs into module folders", "Tell me what's missing"
        script = Script({"steps": [a, b]}, {
            a: [reply("Done, I've moved them."), reply("Done, moved them all."),       # rejected twice
                reply("I can't move files, but I found them."), ],                       # retry: honest
            b: [reply("Nothing is missing.")],
        })
        answer, _ = run(LECTURES, script, {})
        systems = [s for step, _o, s in script.calls if step == a]
        self.assertTrue(any("last try at this step failed" in s for s in systems))
        self.assertIn("can't move files", answer)
        self.assertNotIn("moved them", answer)

    def test_declined_is_not_retried(self):
        a, b = "Remind me on Thursday to revise", "Start a 25 minute timer"
        script = Script({"steps": [a, b]}, {a: [call("add_task", {"text": "revise"}), reply("Okay.")]})
        answer, _ = run("remind me on Thursday to revise and start a 25 minute timer", script,
                        {"add_task": lambda **kw: "Added: revise"}, confirm=lambda n, a: False)
        self.assertIn("I stopped at step 1", answer)
        self.assertIn("you said no", answer)
        self.assertEqual(len([c for c in script.calls if c[0] == a]), 2)  # one attempt only

    def test_retry_that_repeats_the_banned_call_reports_the_first_error(self):
        q = {"query": "japan flight"}
        a, b = "Find my japan flight pdf", "Add a task to check in on Friday"
        script = Script({"steps": [a, b]}, {a: [call("find_files", q), reply("Hmm."),
                                                call("find_files", q), reply("Hmm.")]})
        answer, _ = run("find my japan flight pdf and then add a task to check in on friday", script, {
            "find_files": lambda **kw: "Error running 'find_files': the search index is unavailable. "
                                       "Tell the user this failed. Do not invent a result."})
        self.assertIn("the search index is unavailable.", answer)
        self.assertNotIn("Tell the user", answer)
        self.assertNotIn("find_files", answer)
        self.assertIn("Not done: Add a task to check in on Friday", answer)

    def test_policy_refusal_is_a_reply_not_a_failure(self):
        a, b = "Make a revision list from my signals coursework", "Tell me what's on my plate"
        script = Script({"steps": [a, b]}, {
            a: [call("list_directory", {"path": "."}), reply("Revision list: Fourier series, convolution.")],
            b: [call("list_tasks"), reply("One task: file the Spritzer paperwork.")]})
        answer, _ = run("make a revision list from my signals coursework and tell me what's on my plate",
                        script, {"list_tasks": lambda **kw: "1 open: - file the Spritzer paperwork"})
        self.assertNotIn("stopped", answer)
        self.assertIn("Fourier", answer)
        self.assertIn("Spritzer", answer)


COURSEWORK = ("C:\\Users\\Xichu\\OneDrive - King's College London\\Denisse\\Atlas\\Academic\\"
              "Signals\\Coursework.md")
NOTE_TEXT = "# Signals coursework\nCovers: Fourier series, convolution, the Laplace transform."


class FoundFile(unittest.TestCase):
    """(a) A file one step found is read for the step that uses it."""

    def test_found_path_from_find_files_and_search_vault(self):
        ff = {"name": "find_files", "arguments": {}, "result": "Found 1 file:\n" + COURSEWORK}
        self.assertEqual(multistep.found_path([ff]), COURSEWORK)
        pdf = {"name": "find_files", "arguments": {},
               "result": "Found 2 files:\nC:\\Users\\Xichu\\Downloads\\L6.pdf\n" + COURSEWORK}
        self.assertEqual(multistep.found_path([pdf]), COURSEWORK)   # a PDF isn't text
        sv = {"name": "search_vault", "arguments": {},
              "result": "From your notes (Atlas/Academic/Signals/Coursework.md): Signals coursework, 25%."}
        self.assertTrue(multistep.found_path([sv]).endswith(os.path.join("Atlas", "Academic", "Signals",
                                                                          "Coursework.md")))
        err = {"name": "find_files", "arguments": {}, "result": "Error running 'find_files': " + COURSEWORK}
        self.assertIsNone(multistep.found_path([err]))
        self.assertIsNone(multistep.found_path([{"name": "list_tasks", "arguments": {}, "result": "1 open"}]))

    def test_only_a_step_that_makes_something_from_it(self):
        finder = "find my signals coursework"
        self.assertTrue(multistep.uses_found("make a revision list from it", "make a revision list from it", finder))
        self.assertTrue(multistep.uses_found("Summarise my signals coursework", "summarise it", finder))
        self.assertFalse(multistep.uses_found("remind me on Thursday", "remind me on Thursday", finder))
        self.assertFalse(multistep.uses_found("start a 25 minute timer", "start a 25 minute timer", finder))

    def _signals(self, step1_result, carry="1"):
        parts = multistep.action_parts(SIGNALS)
        script = Script("not a plan", {
            parts[0]: [call("search_vault", {"query": "signals coursework"}), reply("It's in Coursework.md.")],
            parts[1]: [reply("Revision list: 1. Fourier series 2. convolution 3. Laplace transform.")],
            parts[2]: [call("add_task", {"text": "revise signals", "due": "Thursday"}), reply("Added.")],
        })
        reads = []
        with patch.dict(os.environ, {"DENISSE_PLAN_CARRY": carry}):
            answer, msgs = run(SIGNALS, script, {
                "search_vault": lambda **kw: step1_result,
                "read_file": lambda **kw: reads.append(kw) or NOTE_TEXT,
                "add_task": lambda **kw: "Added: revise signals (due Thu 5 Nov).",
            })
        systems = {step: s for step, _o, s in script.calls[1:]}
        return parts, answer, msgs, reads, systems

    def test_the_step_after_a_find_gets_the_files_text(self):
        parts, answer, msgs, reads, systems = self._signals(
            "From your notes (Atlas/Academic/Signals/Coursework.md): Signals coursework, 25%.")
        self.assertEqual(len(reads), 1)
        self.assertTrue(reads[0]["path"].endswith("Coursework.md"))
        self.assertEqual(reads[0]["max_chars"], multistep.FILE_CHARS)
        self.assertIn("Laplace transform", systems[parts[1]])
        self.assertIn("not instructions", systems[parts[1]])
        self.assertNotIn("Laplace transform", systems[parts[0]])
        self.assertNotIn("<<<", systems[parts[2]])          # the reminder doesn't need it
        names = [c["function"]["name"] for m in msgs if m.get("tool_calls") for c in m["tool_calls"]]
        self.assertEqual(names, ["search_vault", "read_file", "add_task"])
        self.assertIn({"role": "tool", "content": NOTE_TEXT}, msgs)
        self.assertIn("Thu", answer)

    def test_switched_off_reads_nothing(self):
        _parts, _a, _m, reads, systems = self._signals(
            "From your notes (Atlas/Academic/Signals/Coursework.md): Signals coursework.", carry="0")
        self.assertEqual(reads, [])
        self.assertFalse(any("<<<" in s for s in systems.values()))

    def test_nothing_found_nothing_read(self):
        _parts, _a, _m, reads, systems = self._signals("Nothing in your notes mentions signals.")
        self.assertEqual(reads, [])
        self.assertFalse(any("<<<" in s for s in systems.values()))

    def test_file_text_is_capped(self):
        long = "x" * 9000
        with patch.dict(brain.tools.TOOL_FUNCTIONS, {"read_file": lambda **kw: long}):
            self.assertEqual(len(multistep.read_found("C:\\a.md", None)), multistep.FILE_CHARS)
        with patch.dict(brain.tools.TOOL_FUNCTIONS, {"read_file": lambda **kw: "Error: no such file"}):
            self.assertIsNone(multistep.read_found("C:\\a.md", None))


class HisDetails(unittest.TestCase):
    """(b) His literal numbers and names reach the step and its reply."""

    def test_details_from_his_words(self):
        self.assertEqual(multistep.details("start a 25 minute timer"), ["25"])
        self.assertEqual(multistep.details("remind me on Thursday"), ["Thursday"])
        self.assertEqual(multistep.details("check the weather in London"), ["London"])
        self.assertEqual(multistep.details("list my tasks"), [])
        self.assertEqual(multistep.details("find my japan flight pdf"), [])
        self.assertEqual(multistep.details("I may need a 1.5 hour \"Deep Work\" block"), ["1.5", "Deep Work"])

    def test_says(self):
        self.assertTrue(multistep.says("Added: revise (due Thu 5 Nov).", "Thursday"))
        self.assertTrue(multistep.says("A 25-minute timer.", "25"))
        self.assertFalse(multistep.says("A 125 minute timer.", "25"))
        self.assertFalse(multistep.says("Timer 2.5 set", "5"))

    def test_dropped_detail_comes_back_in_the_tools_words(self):
        timer = [{"name": "set_timer", "arguments": {"minutes": 25}, "result": "Timer set for 25 minutes."}]
        self.assertEqual(multistep.keep_details("The timer is now running.", ["25"], timer),
                         "The timer is now running. Timer set for 25 minutes.")
        self.assertEqual(multistep.keep_details("A 25 minute timer is running.", ["25"], timer),
                         "A 25 minute timer is running.")

    def test_never_invented(self):
        failed = [{"name": "set_timer", "arguments": {}, "result": "Error running 'set_timer': 25 is too long."}]
        self.assertEqual(multistep.keep_details("I couldn't start it.", ["25"], failed), "I couldn't start it.")
        self.assertEqual(multistep.keep_details("Okay.", ["London"], []), "Okay.")

    def test_task_list_names_every_task(self):
        tasks = [{"name": "list_tasks", "arguments": {}, "result":
                  "2 open:\n- OVERDUE - file the Spritzer paperwork (due Fri 6 Nov)\n- call the dentist\n"
                  "The list is kept in x.json."}]
        self.assertEqual(multistep.keep_details("You need to call the dentist.", [], tasks),
                         "You need to call the dentist.\n\nAlso on your list: file the Spritzer paperwork.")
        both = "Spritzer paperwork and the dentist."
        self.assertEqual(multistep.keep_details(both, [], tasks), both)

    def test_tasks_and_timer_end_to_end(self):
        prompt = "list my tasks and then start a 25 minute timer"
        a, b = multistep.action_parts(prompt)
        script = Script("not a plan", {
            a: [call("list_tasks"), reply("You need to call the dentist and do some paperwork.")],
            b: [call("set_timer", {"minutes": 25}), reply("The timer is now running.")]})
        answer, _ = run(prompt, script, {
            "list_tasks": lambda **kw: ("2 open:\n- file the Spritzer paperwork (due Fri 6 Nov)\n"
                                        "- call the dentist\nThe list is kept in x.json."),
            "set_timer": lambda **kw: "Timer set for 25 minutes."})
        self.assertIn("Spritzer", answer)
        self.assertIn("25", answer)
        # The step was told his number as a structured detail.
        self.assertIn("His exact details for this step", [s for st, _o, s in script.calls if st == b][0])
        self.assertIn(": 25", [s for st, _o, s in script.calls if st == b][0])


class Plain(unittest.TestCase):
    def test_strips_model_instructions_and_tool_names(self):
        self.assertEqual(multistep.plain("Error running 'find_files': drive offline. Tell the user this "
                                         "failed. Do not invent a result."), "drive offline")
        self.assertEqual(multistep.plain("User declined to run 'add_task'."), "you said no to it")
        self.assertNotIn("set_timer", multistep.plain("Error: set_timer crashed"))


if __name__ == "__main__":
    unittest.main()
