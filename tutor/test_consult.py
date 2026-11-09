"""tutor/ part 5: the tutor as the specialist other agents consult -- fixture
vault, fake engines, a fake delegate.

Never touches the real vault, memory/, Ollama, Claude or the network: the
vault comes from test_don.DonFixture (through test_teach.TeachFixture), every
engine is a function, and delegate.py is a stand-in module.

    .venv\\Scripts\\python.exe -m unittest discover -s tutor -p "test_*.py" -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import chat, conf, consult, don  # noqa: E402
from tutor import __main__ as cli  # noqa: E402
from tutor.test_teach import TeachFixture  # noqa: E402

CFG = conf.CONFIG
_K = CFG["consult"]
NAME = conf.tutor_name()
QUESTION = "What do the notes say about the region of convergence of the Laplace transform?"


def fake_opinion(log, tag="local", confident=True, no_json=False):
    """An engine that answers the consult prompt from the notes it was given."""
    def call(system, user):
        log.append((tag, user))
        if no_json:
            return "I would rather not."
        notes = user.split("NOTES:\n", 1)[1]
        seen = "Laplace" if "Laplace" in notes else "Fourier"
        return json.dumps({"answer": f"{tag} opinion: the notes cover {seen}; check the ROC before inverting.",
                           "caveats": [f"{tag}: the notes give no example with a two-sided signal"],
                           "confident": confident})
    return call


class ConsultFixture(TeachFixture):
    def setUp(self):
        super().setUp()
        chat.CALLS = {"local": fake_opinion(self.log), "claude": fake_opinion(self.log, "claude")}
        self.delegate._call = fake_opinion(self.log, "claude")  # delegate.run answers consult prompts too

    def ask(self, question=QUESTION, module="signals", **kw):
        return consult.consult(self.paths, question, module, calls=chat.CALLS, today=self.today, **kw)


class OpinionTests(ConsultFixture):
    def test_grounded_in_the_notes_with_sources_and_confidence(self):
        out = self.ask()
        self.assertIn("local opinion", out["answer"])
        self.assertEqual(out["who"], "local")
        self.assertEqual(out["module"], "Signals and Systems")
        self.assertIn(out["confidence"], (_K["confidence"]["labels"]["medium"], _K["confidence"]["labels"]["high"]))
        self.assertTrue(out["sources"], out)
        self.assertTrue(all(s.startswith("[[") for s in out["sources"]))
        self.assertTrue(any("Week 5 tutor" in s for s in out["sources"]), out["sources"])
        self.assertTrue(any("Laplace tricks" in s for s in out["sources"]), out["sources"])
        self.assertEqual(out["notes"], len(out["sources"]))
        self.assertGreater(out["coverage"], 0)
        self.assertEqual(out["caveats"], ["local: the notes give no example with a two-sided signal"])
        # The model was asked once, with the question and exactly those notes.
        self.assertEqual([tag for tag, _ in self.log], ["local"])
        user = self.log[0][1]
        self.assertIn(QUESTION, user)
        self.assertIn("Laplace", user.split("NOTES:\n", 1)[1])
        self.assertLessEqual(user.count("====="), _K["max_notes"] * 2)

    def test_no_notes_means_low_confidence_and_no_model_call(self):
        out = self.ask("quantum chromodynamics and gluon colour charge")
        self.assertEqual(out["answer"], _K["no_notes"].format(
            module=_K["no_notes_module"].format(module="Signals and Systems"), **don.ctx()))
        self.assertEqual(out["confidence"], _K["confidence"]["labels"]["low"])
        self.assertEqual(out["who"], _K["who_none"])
        self.assertEqual(out["sources"], [])
        self.assertEqual(out["caveats"], [_K["caveat_no_notes"]])
        self.assertEqual(self.log, [])

    def test_module_is_worked_out_from_the_question_or_said_unknown(self):
        out = self.ask(module="")  # only Signals has notes on it
        self.assertEqual(out["module"], "Signals and Systems")
        self.assertIn("local opinion", out["answer"])
        out = self.ask(module="basket weaving")
        self.assertEqual(out["answer"], "")
        self.assertEqual(out["who"], _K["who_none"])
        self.assertIn("basket weaving", out["caveats"][0])
        self.assertIn("Signals and Systems", out["caveats"][0])
        self.assertEqual(len(self.log), 1)

    def test_hard_topic_goes_to_claude_at_once(self):
        with unittest.mock.patch.dict(CFG["teach"], {"hard_topics": ["laplace"]}):
            out = self.ask()
        self.assertEqual(out["who"], "claude")
        self.assertEqual([tag for tag, _ in self.log], ["claude"])

    def test_unconfident_local_answer_goes_to_claude_and_is_waited_for(self):
        chat.CALLS["local"] = fake_opinion(self.log, confident=False)
        out = self.ask()
        self.assertEqual(out["who"], "claude")
        self.assertEqual([tag for tag, _ in self.log], ["local", "claude"])
        self.assertIn("claude opinion", out["answer"])
        self.assertNotIn(_K["caveat_not_confident"], out["caveats"])

    def test_local_without_json_falls_back_to_claude(self):
        chat.CALLS["local"] = fake_opinion(self.log, no_json=True)
        out = self.ask()
        self.assertEqual(out["who"], "claude")
        self.assertTrue(any("gave no usable answer" in c for c in out["caveats"]), out["caveats"])

    def test_hand_offs_off_keeps_the_local_answer_and_says_so(self):
        self.delegate.DRY_RUN = True
        chat.CALLS = {"local": fake_opinion(self.log, confident=False)}
        out = self.ask()
        self.assertEqual(out["who"], "local")
        # One level below what the same notes would have earned a confident answer.
        self.assertEqual(out["confidence"], consult.grade(out["coverage"], out["notes"], False))
        self.assertNotEqual(out["confidence"], consult.grade(out["coverage"], out["notes"], True))
        self.assertIn(_K["caveat_fallback_off"].format(engine="Claude"), out["caveats"])
        self.assertIn(_K["caveat_not_confident"], out["caveats"])
        self.assertEqual([tag for tag, _ in self.log], ["local"])

    def test_claude_through_delegate_run_when_no_call_is_injected(self):
        chat.CALLS = {"local": fake_opinion(self.log, confident=False)}
        out = self.ask()
        self.assertEqual(out["who"], "claude")
        self.assertEqual(self.delegate.runs, [CFG["cards"]["engines"]["claude"]["delegate_engines"]])

    def test_every_engine_failing_gives_no_answer_not_an_invented_one(self):
        chat.CALLS = {"local": fake_opinion(self.log, no_json=True), "claude": fake_opinion(self.log, "claude", no_json=True)}
        out = self.ask()
        self.assertEqual(out["answer"], "")
        self.assertEqual(out["who"], _K["who_none"])
        self.assertEqual(len(out["caveats"]), 2)
        self.assertTrue(out["sources"], "the notes it would have used are still named")

    def test_grade(self):
        labels = _K["confidence"]["labels"]
        self.assertEqual(consult.grade(1.0, 0), labels["low"])
        self.assertEqual(consult.grade(1.0, 3), labels["high"])
        self.assertEqual(consult.grade(1.0, 1), labels["medium"])
        self.assertEqual(consult.grade(0.4, 3), labels["medium"])
        self.assertEqual(consult.grade(0.1, 3), labels["low"])
        self.assertEqual(consult.grade(1.0, 3, confident=False), labels["medium"])
        self.assertEqual(consult.grade(0.1, 3, confident=False), labels["low"])


class RecordTests(ConsultFixture):
    def test_recorded_in_the_don_area_and_linked_from_the_hub(self):
        out = self.ask()
        rec = consult.record(self.paths, out, say=self.out)
        data = json.loads(self.read(self.paths.data(_K["sidecar"])))
        self.assertEqual([c["question"] for c in data["consults"]], [QUESTION])
        self.assertEqual(data["consults"][0]["who"], "local")
        note = self.read(rec["note"])
        self.assertIn(f"# Consultations with {NAME}", note)
        self.assertIn(f"## {self.today.isoformat()}: {QUESTION}", note)
        self.assertIn("confidence **", note)
        self.assertIn("local opinion", note)
        self.assertIn("Sources: [[", note)
        self.assertIn("Caveats:", note)
        hub = self.read(don.hub_path(self.paths))
        self.assertIn(f"[[{NAME} - consults]]", hub)
        self.assertIn(rec["note"], rec["touched"])
        # A second consult goes on top; the oldest stays.
        out2 = self.ask("What does Parseval say about the Fourier transform energy?")
        consult.record(self.paths, out2, say=self.out)
        note = self.read(rec["note"])
        self.assertLess(note.index("Parseval"), note.index("region of convergence"))
        code, report = self.contract()
        self.assertEqual(code, 0, report)

    def test_his_edited_note_is_never_overwritten(self):
        rec = consult.record(self.paths, self.ask(), say=self.out)
        with open(rec["note"], "a", encoding="utf-8") as f:
            f.write("\nMy own remark.\n")
        rec2 = consult.record(self.paths, self.ask("Parseval energy Fourier"), say=self.out)
        self.assertNotEqual(rec2["note"], rec["note"])
        self.assertIn("My own remark", self.read(rec["note"]))


class ChatAndCliTests(ConsultFixture):
    def test_chat_tool_speaks_as_the_tutor_with_the_confidence(self):
        out = chat.consult(QUESTION, "signals")
        self.assertTrue(out.startswith(f"{NAME} here. {NAME} says ("), out)
        self.assertIn("confidence, the local model): local opinion", out)
        self.assertIn("Sources: [[", out)
        self.assertIn("Caveats: local:", out)
        self.assertIn(" Tell him this as", out)
        self.assertTrue(os.path.exists(consult.note_path(self.paths)))
        self.assertTrue(chat.consult("", "signals").startswith(f"{NAME} here. There was no question"))
        self.assertEqual(len(self.log), 1)

    def test_explain_carries_the_tutors_confidence(self):
        from tutor.test_teach import fake_engine
        chat.CALLS = {"local": fake_engine(self.log), "claude": fake_engine(self.log, "claude")}
        out = chat.explain("Laplace transform", "signals")
        self.assertIn(f"{NAME} says this with high confidence, from", out)
        self.assertIn("note(s) of his; relay it as", out)

    def test_tool_function_reaches_the_tutor(self):
        import tools
        self.assertTrue(tools.TOOL_FUNCTIONS["tutor_consult"](QUESTION, "signals").startswith(f"{NAME} here."))

    def test_cli_text_json_and_exit_codes(self):
        args = ["--vault", self.vault, "--state", self.state]
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = cli.main(["consult", *QUESTION.split(), "--module", "signals", *args], calls=chat.CALLS)
        self.assertEqual(rc, 0)
        self.assertTrue(out.getvalue().startswith(f"{NAME} says ("), out.getvalue())
        self.assertIn("Sources: [[", out.getvalue())
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = cli.main(["consult", *QUESTION.split(), "--json", *args], calls=chat.CALLS)
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        for key in ("answer", "confidence", "sources", "caveats", "who", "module", "question"):
            self.assertIn(key, data)
        self.assertEqual(data["who"], "local")
        self.assertEqual(len(json.loads(self.read(self.paths.data(_K["sidecar"])))["consults"]), 2)
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = cli.main(["consult", "gluon", "colour", "charge", "--json", *args], calls=chat.CALLS)
        self.assertEqual(rc, 1, "no notes: no answer to act on")
        self.assertEqual(json.loads(out.getvalue())["who"], _K["who_none"])


class NotesOnlyTests(ConsultFixture):
    """The quick path Orchestra takes when the GPU is busy: no engine is called at all."""

    def test_from_notes_quotes_matching_lines_without_any_model(self):
        boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model on the notes-only path"))
        with unittest.mock.patch.object(consult, "ask", boom), unittest.mock.patch.object(consult.model, "ollama_call", boom):
            out = consult.from_notes(self.paths, QUESTION, "signals", today=self.today)
        self.assertTrue(consult.answered(out), out)
        self.assertEqual(out["who"], _K["notes_only"]["who"])
        self.assertIn("Laplace", out["answer"])
        self.assertTrue(out["sources"])
        self.assertEqual(out["caveats"], [_K["notes_only"]["caveat"]])
        self.assertEqual(self.log, [], "no engine was asked")
        full = self.ask()
        self.assertEqual(out["sources"], full["sources"], "the same notes the model would have been given")

    def test_from_notes_with_no_notes_is_no_answer(self):
        out = consult.from_notes(self.paths, "gluon colour charge", today=self.today)
        self.assertFalse(consult.answered(out))
        self.assertEqual(out["who"], _K["who_none"])

    def test_cli_notes_only_prints_json_and_records_nothing(self):
        args = ["--vault", self.vault, "--state", self.state]
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = cli.main(["consult", *QUESTION.split(), "--json", "--notes-only", *args], calls=chat.CALLS)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["who"], _K["notes_only"]["who"])
        self.assertEqual(self.log, [])
        self.assertFalse(os.path.exists(self.paths.data(_K["sidecar"])), "a notes-only answer is not recorded")


if __name__ == "__main__":
    unittest.main()
