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

    def test_no_existing_eval_prompt_is_planned(self):
        from eval.cases import CASES
        planned = [c["id"] for c in CASES if multistep.is_multistep(c["prompt"])
                   and not c["id"].startswith("multi_")]
        self.assertEqual(planned, [])


class PlanSchema(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(multistep.parse_plan('{"steps": ["Find it", "  Remind   me "]}'),
                         ["Find it", "Remind me"])

    def test_rejected(self):
        for raw in ['{"steps": ["only one"]}', json.dumps({"steps": ["s"] * 6}), "not json",
                    '{"steps": "find"}', '{"steps": ["ok", 3]}', '{"steps": ["ok", ""]}',
                    json.dumps({"steps": ["ok", "x" * 201]}), "[]", None]:
            self.assertIsNone(multistep.parse_plan(raw), raw)

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
        script = Script({"steps": ["Play some music", "List my tasks"]},
                        {"Play some music": [call("play_song", {"query": "lofi"}), reply("Playing.")]})
        played = []
        answer, _ = run("list my tasks and then start a 25 minute timer", script, {
            "play_song": lambda **kw: played.append(kw) or "Playing lofi.",
            "list_tasks": lambda **kw: "1 open: - file the Spritzer paperwork",
        })
        offered = script.calls[1][1]
        self.assertNotIn("play_song", offered)
        self.assertEqual(played, [])

    def test_bad_plan_falls_back_to_one_turn(self):
        script = Script("not a plan", {})
        script.steps[SIGNALS] = [reply("Here's what I can do.")]
        answer, _ = run(SIGNALS, script, {})
        self.assertEqual(answer, "Here's what I can do.")
        self.assertEqual([c[0] for c in script.calls], ["<plan>", SIGNALS])

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
        self.assertIn("declined", answer)
        self.assertEqual(len([c for c in script.calls if c[0] == a]), 2)  # one attempt only


if __name__ == "__main__":
    unittest.main()
