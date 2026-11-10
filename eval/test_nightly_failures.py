"""The 9 Nov 2026 nightly failures and the noisy-run fixes (2026-11-10).

No real model, no real GPU, no Task Scheduler:

  * the failing cases, with the model replayed: the scripted replies are
    what llama3.1 actually said on 9 Nov (vault Denisse/Evals/2026-11-09.md),
    and the case now passes on the eval's own fixtures (fake desktop,
    fixture vault) because Denisse catches it, not because the check moved;
  * a busy GPU before the first case defers the run instead of filing a
    noisy report, and so does running on battery;
  * a case whose model call hangs fails as "timed out" inside its limit,
    against a real HTTP server on loopback that never answers;
  * a run out of budget reports the cases it never started.

    .venv\\Scripts\\python.exe -m unittest eval.test_nightly_failures
"""
import argparse
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import run_eval, speed  # noqa: E402
from eval.cases import CASES  # noqa: E402

BY_ID = {c["id"]: c for c in CASES}


def call(name, **args):
    return {"function": {"name": name, "arguments": args}}


class ReplayedModel:
    """Replays scripted model messages in order; records what was sent."""

    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def chat(self, model=None, messages=None, tools=None, **kw):
        self.sent.append({"messages": list(messages or []), "tools": tools})
        if not self.replies:
            raise AssertionError(f"model called {len(self.sent)} times; only "
                                 f"{len(self.sent) - 1} replies were scripted")
        return {"message": self.replies.pop(0)}


_SCRATCH = {}


def setUpModule():
    # Every store a case can write goes to scratch, as in a real run. isolate_state() seeds the
    # scratch facts from the REAL memory/facts.json (right for the nightly eval, wrong in a test:
    # the checks guard refuses it, 10 Nov), so the seed is an empty fact store here.
    import tools
    with patch.object(tools, "_load_facts", return_value={}):
        _SCRATCH["dir"] = run_eval.isolate_state()


class ReplayedFailures(unittest.TestCase):
    """Each case as it failed on 9 Nov, then the reply the net asks for."""

    def run_replayed(self, cid, replies):
        import brain
        fake = ReplayedModel(replies)
        real = brain.ollama
        brain.ollama = fake
        try:
            b = brain.Brain.__new__(brain.Brain)  # skip __init__'s Ollama check
            result = run_eval.run_case(b, BY_ID[cid], [])
        finally:
            brain.ollama = real
        return result, fake

    def nudged(self, fake):
        last = fake.sent[-1]
        return last["tools"] is None and any(
            m.get("role") == "system" and "only described it" in (m.get("content") or "")
            for m in last["messages"])

    def test_look_summarise_copied(self):
        r, fake = self.run_replayed("look_summarise_copied", [
            {"content": "", "tool_calls": [call("look_at_this")]},
            {"content": "It looks like you copied some text about photosynthesis."},   # 9 Nov
            {"content": "Plants turn light energy into chemical energy: they take in carbon dioxide "
                        "and water, give off oxygen and store the energy as glucose."},
        ])
        self.assertTrue(r["passed"], r)
        self.assertTrue(self.nudged(fake), "the retry carries the nudge and offers no tools")
        self.assertIn("Photosynthesis converts light", fake.sent[-1]["messages"][-1]["content"])

    def test_look_what_does_that_mean(self):
        r, fake = self.run_replayed("look_what_does_that_mean", [
            {"content": "", "tool_calls": [call("look_at_this")]},
            {"content": "It looks like you're currently looking at a document called "
                        "\"Algorithms notes.pdf\" in Adobe Acrobat."},                    # 9 Nov
            {"content": "Merge sort splits the list in half, sorts each half and merges them back, "
                        "so it takes about n log n steps."},
        ])
        self.assertTrue(r["passed"], r)
        self.assertTrue(self.nudged(fake))

    def test_look_rewrite_this(self):
        r, fake = self.run_replayed("look_rewrite_this", [
            {"content": "", "tool_calls": [call("look_at_this"), call("read_file", path="Cover letter.docx")]},
            {"content": "The file \"Cover letter.docx\" doesn't seem to be in the path you specified. "
                        "Can you check if it's in the correct location?"},                # 9 Nov
            {"content": "I'm writing to express my strong interest in your summer internship."},
        ])
        self.assertTrue(r["passed"], r)
        self.assertTrue(self.nudged(fake))

    def test_retry_that_still_only_describes_is_not_kept(self):
        r, fake = self.run_replayed("look_summarise_copied", [
            {"content": "", "tool_calls": [call("look_at_this")]},
            {"content": "It looks like you copied some text about photosynthesis."},
            {"content": "You copied a paragraph from a biology text."},
        ])
        self.assertEqual(len(fake.sent), 3, "one retry, never a loop")
        self.assertEqual(r["reply"], "It looks like you copied some text about photosynthesis.")

    def test_reply_that_works_on_the_text_costs_no_retry(self):
        r, fake = self.run_replayed("look_translate_selected", [
            {"content": "", "tool_calls": [call("look_at_this")]},
            {"content": "Le chat dort sur le canapé. Translation: The cat is sleeping on the couch."},  # 9 Nov pass
        ])
        self.assertTrue(r["passed"], r)
        self.assertEqual(len(fake.sent), 2)

    def test_tutor_quiz_signals(self):
        # 9 Nov, both conditions: she set the quiz, then marked it herself in
        # the same turn, then thanked "Don" -- no question ever reached him.
        r, fake = self.run_replayed("tutor_quiz_signals", [
            {"content": "", "tool_calls": [call("tutor_quiz", module="signals")]},
            {"content": "", "tool_calls": [call("tutor_mark_answer", answer="it becomes multiplication")]},
            {"content": "Thanks for the practice quiz, Don. I'll keep working on those signals and "
                        "systems concepts."},
        ])
        self.assertTrue(r["passed"], r)
        self.assertIn("What does convolution in the time domain become", r["reply"])
        mark_result = [m for m in fake.sent[-1]["messages"] if m.get("role") == "tool"][-1]["content"]
        self.assertTrue(mark_result.startswith("Refused: he hasn't answered yet"), mark_result)
        self.assertNotIn("tutor_mark_answer", r["tools"], "the marking never ran")

    def test_mark_answer_still_runs_on_his_own_turn(self):
        import tool_policy
        self.assertIsNone(tool_policy.refuse_after("tutor_mark_answer", {}))
        self.assertIsNone(tool_policy.refuse_after("tutor_mark_answer", {"search_vault": "x"}))
        self.assertIsNotNone(tool_policy.refuse_after("tutor_mark_answer", {"tutor_exam_question": "x"}))

    def test_multi_weather_umbrella_scorer(self):
        # The planner runs this case, so the scorer is checked on the 9 Nov
        # transcript itself: both tools ran, both were reported.
        case = BY_ID["multi_weather_umbrella"]
        reply = ("Looks like it's a bit drizzly in London right now. Got it -- I added \"take an "
                 "umbrella\", due Tuesday 10 Nov, 23:59.")
        self.assertEqual(run_eval.check(case, reply, ["get_weather", "add_task"], [reply]), [])
        # ...and it still fails a reply that never reports the weather.
        self.assertTrue(run_eval.check(case, "Got it -- I added \"take an umbrella\".",
                                       ["get_weather", "add_task"], []))


class QuietWait(unittest.TestCase):
    def snaps(self, *utils):
        seq = list(utils)

        def snapshot():
            u = seq.pop(0) if len(seq) > 1 else seq[0]
            return {"util_pct": u, "noisy": u is not None and u >= 30}
        return snapshot

    def wait(self, snapshot, wait_s=60):
        clock = {"t": 0.0}
        return speed.wait_for_quiet(wait_s, 10, 2, limit=30, snapshot=snapshot,
                                    sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                                    clock=lambda: clock["t"])

    def test_busy_throughout_is_not_quiet(self):
        got = self.wait(self.snaps(100))
        self.assertFalse(got["quiet"])
        self.assertLessEqual(got["waited_s"], 60)

    def test_goes_quiet_in_time(self):
        self.assertTrue(self.wait(self.snaps(98, 100, 5, 3))["quiet"])

    def test_one_quiet_blip_is_not_enough(self):
        self.assertFalse(self.wait(self.snaps(98, 5, 99, 100, 97, 100, 99, 98), wait_s=50)["quiet"])

    def test_no_nvidia_smi_runs_as_before(self):
        self.assertTrue(self.wait(self.snaps(None))["quiet"])


class Deferral(unittest.TestCase):
    def args(self, **kw):
        return argparse.Namespace(force=False, quiet=True, no_dataset=True, json=None, **kw)

    def test_busy_gpu_start_defers_instead_of_running(self):
        import conversation
        out = Path(tempfile.mkdtemp(prefix="denisse-defer-"))
        busy = {"util_pct": 100, "noisy": True, "quiet": False, "waited_s": 600.0}
        with patch.object(run_eval, "isolate_state", lambda: out / "tasks.json"), \
                patch.object(conversation, "get_brain", lambda: object()), \
                patch.object(conversation, "replay", lambda: []), \
                patch.object(speed, "wait_for_quiet", lambda *a, **k: busy), \
                patch.object(run_eval, "run_case", side_effect=AssertionError("a case ran")):
            code = run_eval._run(self.args(), CASES[:2], None, out, None)
        self.assertEqual(code, run_eval.DEFERRED)
        self.assertEqual(list(out.glob("*.md")), [], "a deferred run files no report")

    def test_on_battery_defers_before_the_lease(self):
        with patch.dict(os.environ, {"ORCHESTRA_POWER": "battery:80"}), \
                patch.object(sys, "argv", ["run_eval.py", "--only", "greet_hey"]), \
                patch.object(run_eval, "_gpu_lease", side_effect=AssertionError("took the lease")):
            self.assertEqual(run_eval.main(), run_eval.DEFERRED)

    def test_deferred_is_the_lease_gate_code(self):
        self.assertEqual(run_eval.DEFERRED, 3)  # run_pass_k / eval_time_parse read 3 as "not a run"


class _NeverAnswers:
    """A loopback HTTP server that accepts and then says nothing."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.conns, self.stop = [], threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                c, _ = self.sock.accept()
                self.conns.append(c)
            except socket.timeout:
                continue
            except OSError:
                return  # closed

    def close(self):
        self.stop.set()
        for c in self.conns:
            c.close()
        self.sock.close()


class HungCase(unittest.TestCase):
    def setUp(self):
        self.server = _NeverAnswers()
        self.env = patch.dict(os.environ, {"OLLAMA_HOST": f"http://127.0.0.1:{self.server.port}"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.close()
        run_eval._CASE_LIMIT["s"] = None

    def test_hung_model_call_times_out_cleanly(self):
        import brain
        import ollama
        real_chat = ollama.chat
        run_eval._CASE_LIMIT["s"] = 1.5
        t0 = time.monotonic()
        r = run_eval.run_case(brain.Brain.__new__(brain.Brain), BY_ID["small_talk"], [])
        took = time.monotonic() - t0
        self.assertFalse(r["passed"])
        self.assertEqual(r["failures"], ["timed out after 1.5s (eval.json case_timeout_s)"])
        self.assertLess(took, 15, "the limit held")
        self.assertIs(ollama.chat, real_chat, "the deadline wrapper is removed after the case")

    def test_no_call_starts_past_the_deadline(self):
        import ollama
        with run_eval.case_deadline(0):
            with self.assertRaises(run_eval.CaseTimeout):
                ollama.chat(model="x", messages=[])


class Budget(unittest.TestCase):
    def test_cases_past_the_budget_are_reported_not_run(self):
        import conversation
        out = Path(tempfile.mkdtemp(prefix="denisse-budget-"))
        quiet = {"util_pct": 0, "used_mib": 0, "total_mib": 8151, "noisy": False, "quiet": True, "waited_s": 0.0}
        args = argparse.Namespace(force=False, quiet=True, no_dataset=True, json=None)
        with patch.object(run_eval, "isolate_state", lambda: out / "tasks.json"), \
                patch.object(run_eval, "reseed_tasks", lambda p: None), \
                patch.object(conversation, "get_brain", lambda: object()), \
                patch.object(conversation, "replay", lambda: []), \
                patch.object(speed, "wait_for_quiet", lambda *a, **k: quiet), \
                patch.dict(run_eval.CFG, {"run_budget_s": 0}), \
                patch.object(run_eval, "run_case", side_effect=AssertionError("a case ran")):
            code = run_eval._run(args, CASES[:2], {"ref": "--only", "reasons": ["test"]}, out, None)
        self.assertEqual(code, 1)
        text = next(out.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("cut short: 4 case run(s) not started", text)
        self.assertIn("not run: the run's 0s budget", text)


class Config(unittest.TestCase):
    def test_limits_fit_the_scheduled_task(self):
        cfg = speed.eval_config()
        s = cfg["scheduled"]
        self.assertLess(cfg["run_budget_s"] + 60 * s["ollama_wait_min"], s["task_limit_s"])
        self.assertLess(cfg["gpu"]["lease_wait_s"] + cfg["gpu"]["quiet_wait_s"], cfg["run_budget_s"])
        self.assertGreaterEqual(cfg["gpu"]["lease_ttl_s"], cfg["run_budget_s"])


if __name__ == "__main__":
    unittest.main()
