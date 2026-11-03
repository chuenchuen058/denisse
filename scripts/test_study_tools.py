"""Study mode as the model sees it (2026-11-03): the clock's pause/resume,
the five tool functions, the prompt context line, and the brain acting on
the clock through them. Headless -- no Tk window, no chime, no model.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import brain  # noqa: E402
import study_mode  # noqa: E402
import tools  # noqa: E402


class NoTkRoot:
    def __init__(self):
        self.jobs = []

    def after(self, ms, fn):
        self.jobs.append(fn)
        return len(self.jobs)

    def after_cancel(self, job):
        pass


def headless():
    return study_mode.StudyMode(NoTkRoot(), lambda *a, **k: None)


def user(text):
    return {"role": "user", "content": text}


def reply(text):
    return {"message": {"content": text}}


def call(name, args=None):
    return {"message": {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}}


class Clock(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(study_mode, "_chime", lambda: None),
                        patch.object(study_mode, "_CURRENT", None),
                        patch.object(study_mode, "_dispatch", None)]
        for p in self.patches:
            p.start()
        self.mode = headless()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_tools_with_nothing_running_change_nothing(self):
        self.assertEqual(study_mode.session_status(), "No study session is running.")
        self.assertIn("nothing was paused", study_mode.pause_session())
        self.assertIn("nothing was resumed", study_mode.resume_session())
        self.assertIn("nothing was stopped", study_mode.stop_session())
        self.assertFalse(self.mode.active)
        self.assertEqual(study_mode.context_line(), "")
        for r in (study_mode.pause_session(), study_mode.resume_session(), study_mode.stop_session()):
            self.assertTrue(brain._confirms_nothing(r), r)

    def test_start_pause_resume_stop(self):
        started = study_mode.start_session()
        self.assertIn("25 minutes", started)
        self.assertTrue(self.mode.active and not self.mode.paused)
        self.assertIn("Study session running", study_mode.context_line())
        self.assertIn("Already running", study_mode.start_session())
        paused = study_mode.pause_session()
        self.assertTrue(paused.startswith("Study session paused with"), paused)
        self.assertTrue(self.mode.paused)
        self.assertEqual(self.mode.status(), "Paused · 25 min left")
        self.assertIn("already paused", study_mode.pause_session())
        # The tick does nothing while paused, even past the old deadline.
        self.mode.ends_at -= dt.timedelta(minutes=30)
        self.mode._check()
        self.assertEqual(self.mode.phase, "study")
        self.assertEqual(self.mode.round_no, 1)
        resumed = study_mode.resume_session()
        self.assertIn("resumed", resumed)
        self.assertFalse(self.mode.paused)
        self.assertGreater((self.mode.ends_at - dt.datetime.now()).total_seconds(), 24 * 60)
        self.assertIn("wasn't paused", study_mode.resume_session())
        stopped = study_mode.stop_session()
        self.assertIn("stopped after 1 round", stopped)
        self.assertFalse(self.mode.active)
        self.assertIsNone(self.mode._job)

    def test_on_change_hook_fires(self):
        seen = []
        self.mode.on_change = lambda m: seen.append(m.status() or "off")
        self.mode.start()
        self.mode.pause()
        self.mode.resume()
        self.mode.stop()
        self.assertEqual(len(seen), 4)
        self.assertEqual(seen[-1], "off")
        self.assertTrue(seen[1].startswith("Paused"))

    def test_dispatcher_is_used(self):
        used = []

        def dispatch(fn):
            used.append(fn)
            return fn()
        study_mode.set_dispatcher(dispatch)
        study_mode.start_session()
        study_mode.session_status()
        self.assertEqual(len(used), 2)

    def test_tool_functions_are_registered(self):
        for name in ("study_session_status", "start_study_session", "pause_study_session",
                     "resume_study_session", "stop_study_session"):
            self.assertIn(name, tools.TOOL_FUNCTIONS)
            self.assertIn(name, {d["function"]["name"] for d in brain.TOOL_DEFINITIONS})
        self.assertEqual(tools.TOOL_FUNCTIONS["study_session_status"](), "No study session is running.")

    def test_whole_message_phrases_still_work_from_the_policy(self):
        for text, want in (("study mode", "study"), ("pomodoro", "study"), ("study session over", "stop_study"),
                           ("what is the pomodoro technique?", None)):
            self.assertEqual(study_mode.match_command(text, active=False), want, text)
        self.assertEqual(study_mode.match_command("turn it off", active=True), "stop_study")
        self.assertIsNone(study_mode.match_command("turn it off", active=False))


class BrainOnTheClock(unittest.TestCase):
    """The brain, with a scripted model, driving the headless clock."""

    def run_turn(self, prompt, responses, before=None):
        bot = brain.Brain.__new__(brain.Brain)
        it = iter(responses)
        prompts = []

        def chat(**kwargs):
            prompts.append(kwargs["messages"][0]["content"])
            return next(it, reply("Done."))
        with patch.object(brain.ollama, "chat", side_effect=chat):
            out = bot.respond(list(before or []) + [user(prompt)])[0]
        return out, prompts

    def setUp(self):
        self.patches = [patch.object(study_mode, "_chime", lambda: None),
                        patch.object(study_mode, "_CURRENT", None),
                        patch.object(study_mode, "_dispatch", None)]
        for p in self.patches:
            p.start()
        self.mode = headless()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_session_over_stops_the_clock(self):
        self.mode.start()
        out, prompts = self.run_turn("session over", [call("stop_study_session"), reply("Done -- session ended after 1 round.")])
        self.assertFalse(self.mode.active)
        self.assertIn("Study session running", prompts[0])  # the model was told the state

    def test_typo_pause_pauses_the_study_clock(self):
        self.mode.start()
        out, _ = self.run_turn("pause sutuy timer", [call("pause_study_session"), reply("Paused -- 25 minutes left.")])
        self.assertTrue(self.mode.paused)
        self.assertIn("paused", out.lower())

    def test_pause_with_nothing_running_cannot_be_reported_as_paused(self):
        out, _ = self.run_turn("pause the study timer", [call("pause_study_session"), reply("Study timer paused."),
                                                         reply("Study timer paused.")])
        self.assertFalse(self.mode.active)
        self.assertNotIn("timer paused", out.lower())
        self.assertIn("nothing was paused", out.lower())

    def test_status_question_is_answered_from_the_clock(self):
        self.mode.start()
        out, _ = self.run_turn("is the study session running?",
                               [call("study_session_status"), reply("Yes -- 25 minutes left of this study block.")])
        self.assertIn("25", out)
        self.assertTrue(self.mode.active and not self.mode.paused)


if __name__ == "__main__":
    unittest.main(verbosity=2)
