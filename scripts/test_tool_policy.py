"""tool_policy.py and the brain's execute() against it -- the 2026-11-03
rewrite of what a tool call may do (AGENTS.md Rule 0).

Each test is one of that evening's failures, replayed with a scripted model
and isolated tools: no Ollama, no real files, no real clock. The point is
that a refusal now comes from the CALL -- its name, side effect and
arguments -- and never from a regex on what Chuen typed.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DENISSE_REPLY_PREFERENCES",
                      os.path.join(tempfile.mkdtemp(prefix="denisse-policy-"), "reply_preferences.json"))

import brain  # noqa: E402
import brain_rules  # noqa: E402
import planner  # noqa: E402
import reply_preferences  # noqa: E402
import tool_policy  # noqa: E402
import tool_router  # noqa: E402

# "yes please" asks planner.pending(); keep that off the real memory folder.
planner.PENDING_FILE = os.path.join(tempfile.mkdtemp(prefix="denisse-policy-"), "pending_plan.json")


def user(text):
    return {"role": "user", "content": text}


def reply(text):
    return {"message": {"content": text}}


def call(name, args=None):
    return {"message": {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}}


def run_turn(prompt, responses, functions=None, before=None):
    """One turn with a scripted model; returns (reply, tool results seen)."""
    bot = brain.Brain.__new__(brain.Brain)
    it = iter(responses)
    seen = []

    def chat(**kwargs):
        for m in kwargs["messages"]:
            if m.get("role") == "tool":
                seen.append(m["content"])
        return next(it, reply("Done."))
    isolated = {name: (lambda **kw: "Refused: isolated tool") for name in brain.tools.TOOL_FUNCTIONS}
    isolated.update(functions or {})
    with patch.object(brain.ollama, "chat", side_effect=chat), \
            patch.dict(brain.tools.TOOL_FUNCTIONS, isolated, clear=True):
        out = bot.respond(list(before or []) + [user(prompt)])[0]
    return out, seen


class PolicyData(unittest.TestCase):
    def test_file_is_valid_and_covers_every_tool(self):
        names = {d["function"]["name"] for d in brain.TOOL_DEFINITIONS}
        self.assertTrue(names <= set(tool_policy.load()["tools"]),
                        sorted(names - set(tool_policy.load()["tools"])))
        self.assertEqual(names, set(brain.tools.TOOL_FUNCTIONS), "definitions and registry disagree")
        for name in names:
            self.assertIn(tool_policy.side_effect(name), ("read", "write"), name)

    def test_mutating_set_is_the_policy(self):
        self.assertEqual(brain_rules.MUTATING_TOOLS, tool_policy.mutating())
        for name in ("add_task", "write_file", "open_application", "pause_music",
                     "pause_study_session", "reply_preference"):
            self.assertIn(name, tool_policy.mutating(), name)
        for name in ("find_files", "get_current_time", "study_session_status", "calculate"):
            self.assertNotIn(name, tool_policy.mutating(), name)

    def test_router_groups_come_from_the_policy(self):
        self.assertEqual(set(tool_router.GROUPS), set(tool_policy.load()["groups"]))
        self.assertIn("study", tool_router.GROUPS)
        offered = {d["function"]["name"] for d in tool_router.select(
            brain.TOOL_DEFINITIONS, [user("pause sutuy timer")])}
        self.assertIn("pause_study_session", offered)
        offered = {d["function"]["name"] for d in tool_router.select(
            brain.TOOL_DEFINITIONS, [user("is the study session running?")])}
        self.assertIn("study_session_status", offered)

    def test_edit_takes_effect_without_restart(self):
        tmp = os.path.join(tempfile.mkdtemp(prefix="denisse-policy-"), "policy.json")
        data = tool_policy.load()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        with patch.dict(os.environ, {"DENISSE_TOOL_POLICY": tmp}):
            self.assertEqual(tool_policy.side_effect("find_files"), "read")
            data = json.loads(json.dumps(data))
            data["tools"]["find_files"]["side_effect"] = "write"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.utime(tmp, (1, 1))  # a different mtime, however fast this ran
            self.assertEqual(tool_policy.side_effect("find_files"), "write")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("{not json")
            os.utime(tmp, (2, 2))
            self.assertEqual(tool_policy.side_effect("find_files"), "write")  # last good kept
        tool_policy.load()  # back to the real file


class CallChecks(unittest.TestCase):
    def test_destructive_shell_refused(self):
        for cmd in ("Remove-Item C:\\x", "del C:\\x.pdf", "Move-Item -Path a -Destination b", "echo hi > C:\\x.txt"):
            self.assertTrue(tool_policy.refuse("run_shell_command", {"command": cmd}), cmd)
        for cmd in ("Get-ChildItem C:\\Users\\Xichu\\Downloads", "ipconfig", "Get-Process > $null"):
            self.assertIsNone(tool_policy.refuse("run_shell_command", {"command": cmd}), cmd)

    def test_document_path_needs_verifying_but_an_app_does_not(self):
        never = lambda p: False  # noqa: E731
        self.assertIsNone(tool_policy.refuse("open_application", {"app_name": "Spotify"}, verified_path=never))
        self.assertIsNone(tool_policy.refuse("open_application", {"app_name": "passport"}, verified_path=never))
        self.assertTrue(tool_policy.refuse("open_application", {"app_name": "C:/x/latest_cv.pdf"}, verified_path=never))
        self.assertTrue(tool_policy.refuse("open_application", {"app_name": "syllabus.pdf"}, verified_path=never))
        self.assertIsNone(tool_policy.refuse("open_application", {"app_name": "C:/x/cv.pdf"},
                                             verified_path=lambda p: p == "C:/x/cv.pdf"))

    def test_unknown_skill_refused_known_allowed(self):
        known = lambda n: n == "organize_files"  # noqa: E731
        self.assertTrue(tool_policy.refuse("read_skill", {"name": "plan_internships"}, known_skill=known))
        self.assertIsNone(tool_policy.refuse("read_skill", {"name": "organize_files"}, known_skill=known))

    def test_read_tools_may_run_unoffered_write_tools_may_not(self):
        self.assertTrue(tool_policy.allowed_unoffered("find_files"))
        self.assertTrue(tool_policy.allowed_unoffered("study_session_status"))
        self.assertFalse(tool_policy.allowed_unoffered("add_task"))
        self.assertFalse(tool_policy.allowed_unoffered("open_application"))
        self.assertFalse(tool_policy.allowed_unoffered("no_such_tool"))


class ExecuteAgainstThePolicy(unittest.TestCase):
    def test_file_lookup_runs_even_when_not_offered(self):
        # 3 Nov: "could you open my signal and system syllabus?" -> "yes
        # please" -> find_files refused as not available for the request.
        search = Mock(return_value="Found 1 file:\nC:\\Users\\Xichu\\Documents\\syllabus.pdf")
        out, seen = run_turn("yes please", [call("find_files", {"query": "syllabus"}), reply("It's syllabus.pdf.")],
                             {"find_files": search},
                             before=[user("could you open my signal and system syllabus?"),
                                     {"role": "assistant", "content": "Do you want me to find it?"}])
        search.assert_called_once()
        self.assertFalse(any(s.startswith("Refused") for s in seen), seen)

    def test_unoffered_write_is_still_refused(self):
        write = Mock(return_value="Added: nothing")
        run_turn("hey", [call("add_task", {"text": "nothing"}), reply("No worries.")], {"add_task": write})
        write.assert_not_called()

    def test_clock_is_the_models_call_not_a_word_list(self):
        # 3 Nov: "session over" -> get_current_time -> "No tool needed here."
        clock = Mock(return_value="It's 5:43 PM on Tuesday 3 November 2026.")
        out, seen = run_turn("session over", [call("get_current_time"), reply("It's 5:43 PM.")],
                             {"get_current_time": clock})
        clock.assert_called_once()
        self.assertFalse(any(s.startswith("No tool needed") for s in seen), seen)

    def test_calculate_with_nothing_to_calculate_is_turned_back(self):
        calc = Mock(return_value="100 = 100")
        out, seen = run_turn("how high is your iq", [call("calculate", {"expression": "100"}),
                                                     reply("Hard to say, honestly.")], {"calculate": calc})
        calc.assert_not_called()
        self.assertTrue(any(s.startswith("No tool needed") for s in seen), seen)

    def test_standing_instruction_is_a_preference_not_a_task(self):
        # 31 Oct / 3 Nov: "...ask if i want the file path next time" -> add_task.
        write = Mock(return_value="Added: ask for file path next time (no deadline)")
        text = "you also dont need to pronounce the file path, just ask if i want the file path next time"
        with patch.object(reply_preferences, "FILE", os.path.join(tempfile.mkdtemp(), "p.json")):
            out, _ = run_turn(text, [call("add_task", {"text": "ask for file path next time"})], {"add_task": write})
            self.assertIn("noted", out.lower())
            self.assertIn("file path", reply_preferences.prompt_block().lower())
        write.assert_not_called()

    def test_preference_tool_feeds_the_prompt(self):
        with patch.object(reply_preferences, "FILE", os.path.join(tempfile.mkdtemp(), "p.json")):
            saved = brain.tools.reply_preference("add", "say me instead of Chuen")
            self.assertTrue(saved.startswith("Noted"), saved)
            self.assertIn("say me instead of chuen", reply_preferences.prompt_block().lower())
            self.assertTrue(brain.tools.reply_preference("remove", "the Chuen one").startswith("Forgot"))
            self.assertEqual(reply_preferences.prompt_block(), "")
            # The prompt carries it on the next turn and nothing is persisted into messages.
            brain.tools.reply_preference("add", "keep it short")
            prompts = []

            def chat(**kw):
                prompts.append(kw["messages"][0]["content"])
                return reply("Sure.")
            bot = brain.Brain.__new__(brain.Brain)
            with patch.object(brain.ollama, "chat", side_effect=chat):
                _, msgs = bot.respond([user("tell me a joke")])
            self.assertIn("keep it short", prompts[0])
            self.assertFalse(any("keep it short" in (m.get("content") or "") for m in msgs))

    def test_preference_saved_but_not_acknowledged_gets_the_fast_paths_words(self):
        # Eval 2026-11-03: reply_preference ran and she said "No worries." /
        # "I'll make the change." He asked for "alright noted".
        with patch.object(reply_preferences, "FILE", os.path.join(tempfile.mkdtemp(), "p.json")):
            out, _ = run_turn("could you please always say me rather than Chuen when you read my notes back",
                              [call("reply_preference", {"text": "say me rather than Chuen"}), reply("No worries.")],
                              {"reply_preference": brain.tools.reply_preference})
            self.assertEqual(out, "Alright, noted.")
            self.assertIn("say me rather than chuen", reply_preferences.prompt_block().lower())
            out, _ = run_turn("keep replies shorter please",
                              [call("reply_preference", {"text": "keep replies short"}), reply("Sure, will do.")],
                              {"reply_preference": brain.tools.reply_preference})
            self.assertEqual(out, "Sure, will do.")

    def test_task_confirmation_is_a_sentence_not_the_tool_text(self):
        # 3 Nov: "Added: remind me there is a robotics meeting (today 17:00)" verbatim.
        out, _ = run_turn("remind me there's a robotics meeting at 5pm",
                          [call("add_task", {"text": "robotics meeting", "due": "5pm"}), reply("Done.")],
                          {"add_task": lambda text, due="": "Added: robotics meeting -- due today, 17:00."})
        self.assertNotIn("Added:", out)
        self.assertIn("robotics meeting", out)
        self.assertIn("17:00", out)

    def test_natural_task_reply_kept_when_its_dates_are_real(self):
        out, _ = run_turn("remind me to email Ben on friday",
                          [call("add_task", {"text": "email Ben", "due": "friday"}),
                           reply("Got it -- I'll remind you to email Ben on Friday.")],
                          {"add_task": lambda text, due="": "Added: email Ben -- due Friday 6 Nov, 23:59."})
        self.assertEqual(out, "Got it -- I'll remind you to email Ben on Friday.")

    def test_invented_date_in_task_reply_is_replaced(self):
        out, _ = run_turn("remind me to submit the sign up",
                          [call("add_task", {"text": "submit the sign up"}),
                           reply("I'll remind you on the 6th of October at 2 PM.")],
                          {"add_task": lambda text, due="": "Added: submit the sign up (no deadline)"})
        self.assertNotIn("October", out)
        self.assertIn("submit the sign up", out)

    def test_false_success_over_a_tool_that_did_nothing(self):
        # 3 Nov: pause_music ran, and she said "Sutuy timer paused."
        out, _ = run_turn("pause sutuy timer",
                          [call("pause_music"), reply("Sutuy timer paused."), reply("Sutuy timer paused.")],
                          {"pause_music": lambda: "Paused."})
        self.assertNotIn("timer paused", out.lower())

    def test_insult_is_a_complaint_and_reruns_the_request(self):
        status = Mock(return_value="Study session running: studying, 18 min left of this study block, round 1.")
        out, _ = run_turn("omg youre so dumb",
                          [call("study_session_status"), reply("Yes -- 18 minutes left of this study block.")],
                          {"study_session_status": status},
                          before=[user("is the study session running?"),
                                  {"role": "assistant", "content": "There are no timers running."}])
        self.assertNotIn("what did you mean", out.lower())
        self.assertIn("you asked", out.lower())
        status.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
