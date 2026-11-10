"""tutor/learn.py: Claude's corrections kept, shown back to the local model
as few-shot examples, and counted in the weekly report; the bench eval's
with-vs-without plumbing.

Fixture vault and fake models only (test_teach.TeachFixture): no real vault,
no Ollama, no Claude, no embeddings call.

    .venv\\Scripts\\python.exe -m unittest tutor.test_learn -v
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import cards, chat, conf, consult, learn, sources, teach  # noqa: E402
from tutor import __main__ as cli  # noqa: E402
from tutor.test_consult import QUESTION, ConsultFixture, fake_opinion  # noqa: E402
from tutor.test_teach import TeachFixture, fake_engine  # noqa: E402

sys.path.insert(0, os.path.join(REPO, "scripts"))
import eval_local_learning as bench_eval  # noqa: E402

_L = conf.CONFIG["learn"]
_K = conf.CONFIG["cards"]["check"]


def recording(inner, systems):
    """`inner`, with every system prompt it was given kept in `systems`."""
    def call(system, user):
        systems.append(system)
        return inner(system, user)
    return call


def rec(job="teach", inp="Gauss's law for a sphere", accepted="the accepted lesson", why="few_steps",
        when="2026-11-09T10:00:00", model="m1", **more):
    return {"when": when, "job": job, "model": model, "input": inp, "local": "thin", "why": why,
            "accepted": accepted, **more}


class StoreTests(TeachFixture):
    def test_capture_writes_one_private_record_with_the_pair(self):
        out = learn.capture(self.paths, "teach", "Fourier transform", {"steps": []}, "few_steps",
                            {"steps": ["a", "b"]}, model="m1", engine="claude", module="Signals and Systems")
        self.assertEqual(learn.store_path(self.paths), self.paths.data(_L["store_file"]))
        self.assertTrue(learn.store_path(self.paths).startswith(self.vault))  # Don's data area, not the repo
        [got] = learn.load(self.paths)
        self.assertEqual(got, out)
        self.assertEqual((got["job"], got["model"], got["why"], got["input"]),
                         ("teach", "m1", "few_steps", "Fourier transform"))
        self.assertEqual(json.loads(got["accepted"]), {"steps": ["a", "b"]})

    def test_store_is_trimmed_per_job_newest_kept(self):
        keep = _L["keep_per_job"]
        for i in range(keep + 3):
            learn.capture(self.paths, "teach", f"topic {i}", "", "few_steps", "x", model="m")
        learn.capture(self.paths, "consult", "q", "", "empty", "y", model="m")
        teach_recs = learn.load(self.paths, "teach")
        self.assertEqual(len(teach_recs), keep)
        self.assertEqual(teach_recs[-1]["input"], f"topic {keep + 2}")
        self.assertEqual(len(learn.load(self.paths, "consult")), 1)

    def test_broken_lines_and_missing_store_are_empty_not_errors(self):
        self.assertEqual(learn.load(self.paths), [])
        os.makedirs(self.paths.data_dir, exist_ok=True)
        with open(learn.store_path(self.paths), "w", encoding="utf-8") as f:
            f.write("not json\n" + json.dumps(rec()) + "\n")
        self.assertEqual(len(learn.load(self.paths)), 1)

    def test_embeddings_are_kept_when_an_embed_function_answers(self):
        got = learn.capture(self.paths, "teach", "x y", "", "few_steps", "z", model="m", embed=lambda t: [1.0, 0.0])
        self.assertEqual(got["vec"], [1.0, 0.0])
        broken = learn.capture(self.paths, "teach", "x y", "", "few_steps", "z", model="m",
                               embed=lambda t: (_ for _ in ()).throw(OSError("no embed model")))
        self.assertNotIn("vec", broken)


class ShotTests(unittest.TestCase):
    def test_most_similar_accepted_examples_first_and_capped(self):
        records = [rec(inp="Laplace transform of a step", accepted="L1"),
                   rec(inp="Gauss's law for a charged sphere", accepted="G1"),
                   rec(inp="Gauss's law for a line of charge", accepted="G2"),
                   rec(inp="Gauss's law for a charged shell", accepted=None),  # no accepted answer: never a shot
                   rec(job="consult", inp="Gauss's law for a charged sphere", accepted="C1")]
        got = [r["accepted"] for _s, r in learn.similar(records, "teach", "Gauss's law for a charged shell")]
        self.assertEqual(got[0], "G1")
        self.assertNotIn("L1", got)
        self.assertNotIn("C1", got)
        self.assertLessEqual(len(got), _L["shots"]["max"])
        block = learn.block(records, "teach", "Gauss's law for a charged shell")
        self.assertTrue(block.startswith(_L["shots"]["intro"]))
        self.assertIn("G1", block)
        self.assertEqual(learn.block(records, "teach", "Bode plots"), "")

    def test_budget_is_respected_and_examples_are_whole(self):
        big = "x" * (_L["shots"]["budget_chars"] - 50)
        records = [rec(inp="Gauss's law sphere", accepted=big), rec(inp="Gauss's law sphere charge", accepted="small")]
        block = learn.block(records, "teach", "Gauss's law sphere charge")
        self.assertLessEqual(len(block) - len(_L["shots"]["intro"]), _L["shots"]["budget_chars"])
        self.assertIn("small", block)

    def test_embeddings_rank_when_both_sides_have_them(self):
        records = [rec(inp="alpha", accepted="A", vec=[1.0, 0.0]), rec(inp="beta", accepted="B", vec=[0.0, 1.0])]
        got = learn.similar(records, "teach", "anything", embed=lambda t: [0.9, 0.1])
        self.assertEqual([r["accepted"] for _s, r in got], ["A"])


class TeachLearningTests(TeachFixture):
    def test_failed_self_check_then_claude_lesson_is_captured_and_shown_next_time(self):
        systems = []
        chat.CALLS["local"] = recording(fake_engine(self.log, thin=True), systems)
        chat.explain("Fourier transform", "signals")
        [got] = learn.load(self.paths, teach.JOB)
        self.assertEqual(got["input"], "Fourier transform")
        self.assertEqual(got["why"], "few_steps")
        self.assertEqual(got["engine"], "claude")
        self.assertIn("claude summary of Fourier transform", got["accepted"])
        self.assertIn("thinly", got["local"])
        self.assertNotIn(_L["shots"]["intro"], systems[0])  # nothing learnt before the first call
        chat.explain("Fourier transform", "signals")
        self.assertIn(_L["shots"]["intro"], systems[1])
        self.assertIn("claude summary of Fourier transform", systems[1])

    def test_a_lesson_that_stands_or_a_hard_topic_is_not_a_correction(self):
        chat.explain("Laplace transform", "signals")
        self.assertEqual(learn.load(self.paths), [])
        teach.learn_from(self.paths, self.ss_module, "x", {"why": "hard", "lesson": None}, {"summary": "s"}, "claude")
        self.assertEqual(learn.load(self.paths), [])

    def test_claude_failing_too_is_counted_without_an_accepted_answer(self):
        chat.CALLS["local"] = fake_engine(self.log, thin=True)
        chat.CALLS["claude"] = fake_engine(self.log, "claude", no_json=True)
        chat.explain("Fourier transform", "signals")
        [got] = learn.load(self.paths)
        self.assertIsNone(got["accepted"])

    def test_check_failure_names_the_reason(self):
        ok = {"summary": "s", "steps": ["x" * 50, "y" * 50], "question": "q", "answer": "a", "confident": True}
        self.assertIsNone(teach.check_failure(ok))
        self.assertEqual(teach.check_failure({**ok, "summary": ""}), "no_summary")
        self.assertEqual(teach.check_failure({**ok, "steps": ["short"]}), "few_steps")
        self.assertEqual(teach.check_failure({**ok, "question": ""}), "no_question")
        self.assertEqual(teach.check_failure({**ok, "confident": False}), "not_confident")
        for key in ("no_summary", "few_steps", "no_question", "not_confident"):
            self.assertIn(key, _L["reasons"]["teach"])


class ConsultLearningTests(ConsultFixture):
    def test_unsure_local_opinion_and_claudes_answer_are_captured_then_shown(self):
        systems = []
        chat.CALLS["local"] = recording(fake_opinion(self.log, confident=False), systems)
        out = consult.consult(self.paths, QUESTION, "signals", calls=chat.CALLS, today=self.today)
        self.assertEqual(out["who"], "claude")
        [got] = learn.load(self.paths, consult.JOB)
        self.assertEqual(got["why"], "not_confident")
        self.assertIn("claude opinion", got["accepted"])
        consult.consult(self.paths, QUESTION, "signals", calls=chat.CALLS, today=self.today)
        self.assertNotIn(_L["shots"]["intro"], systems[0])
        self.assertIn("claude opinion", systems[1])

    def test_a_confident_local_opinion_is_not_captured(self):
        self.ask()
        self.assertEqual(learn.load(self.paths), [])


class CardLearningTests(TeachFixture):
    def setUp(self):
        super().setUp()
        self.src = next(iter(sources.collect(self.paths, self.ss_module)))
        self.text = " ".join(self.src.text.split())

    def card(self):
        return {"id": "c1", "kind": "method", "front": "How do you find the transform?", "back": "Guess.",
                "topic": "Laplace transform", "source": self.src.rel, "source_name": self.src.stem,
                "week": self.src.week, "engine": "local"}

    def checker(self, answer):
        def call(system, user):
            return json.dumps({"checks": [{"n": 1, **answer}]})
        return call

    def test_a_local_card_the_checker_corrected_is_captured_with_the_correction(self):
        evidence = " ".join(self.text.split()[:12])
        answer = {"verdict": _K["wrong"], "reason": "the back does not answer the front",
                  "corrected": "Apply the definition, then the table.", "evidence": evidence}
        engine = cards.check_engine("method")
        passed, _held, _ = cards.check(self.ss_module, self.src, [self.card()], {},
                                       checks={engine: self.checker(answer)}, paths=self.paths)
        self.assertEqual(passed[0]["check"]["verdict"], _K["verdicts"]["fixed"])
        [got] = learn.load(self.paths, cards.JOB)
        self.assertIn("How do you find the transform?", got["input"])
        self.assertEqual(json.loads(got["local"])["back"], "Guess.")
        self.assertEqual(json.loads(got["accepted"])["back"], "Apply the definition, then the table.")
        self.assertEqual(got["why"], "the back does not answer the front")

    def test_no_paths_or_a_right_card_captures_nothing(self):
        engine = cards.check_engine("method")
        right = self.checker({"verdict": _K["right"], "reason": "fine"})
        cards.check(self.ss_module, self.src, [self.card()], {}, checks={engine: right}, paths=self.paths)
        wrong = self.checker({"verdict": _K["wrong"], "reason": "no"})
        cards.check(self.ss_module, self.src, [self.card()], {}, checks={engine: wrong})
        self.assertEqual(learn.load(self.paths), [])

    def test_local_writer_gets_earlier_card_corrections(self):
        learn.capture(self.paths, cards.JOB, f"method card on {self.src.topic or self.src.stem}: front",
                      "local back", "wrong", {"front": "f", "back": "the corrected back"}, model="m")
        systems = []
        writer = recording(lambda s, u: json.dumps({"cards": []}), systems)
        cards.generate(self.ss_module, self.src, calls={"local": writer}, only={"recall"}, paths=self.paths)
        self.assertIn("the corrected back", systems[0])


class ReportTests(TeachFixture):
    def test_top_reasons_per_job_and_model_in_the_window(self):
        today = dt.date(2026, 11, 10)
        records = [rec(why="few_steps"), rec(why="few_steps"), rec(why="no_question", accepted=None),
                   rec(model="m2", why="not_confident"),
                   rec(job="consult", why="empty"),
                   rec(why="few_steps", when="2026-10-01T10:00:00")]  # outside the 7 days
        rep = learn.report(self.paths, today=today, records=records)
        m1 = rep["jobs"]["teach"]["m1"]
        self.assertEqual((m1["total"], m1["accepted"]), (3, 2))
        self.assertEqual(m1["reasons"][0], ("few_steps", 2))
        self.assertIn("m2", rep["jobs"]["teach"])
        text = learn.render_report(rep)
        self.assertIn(_L["reasons"]["teach"]["few_steps"], text)
        self.assertIn(_L["reasons"]["consult"]["empty"], text)
        self.assertIn(_L["report"]["none"], learn.render_report(learn.report(self.paths, today=today, records=[])))

    def test_cli_prints_the_report(self):
        learn.capture(self.paths, "teach", "t", "", "few_steps", "a", model="m1")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["learned", "--vault", self.vault, "--state", self.state])
        self.assertEqual(rc, 0)
        self.assertIn("teach on m1: 1 failed", buf.getvalue())


class BenchTests(unittest.TestCase):
    def test_fake_model_that_needs_the_examples_passes_only_with_them(self):
        bench = bench_eval.load_bench()
        good_lesson = {"summary": "s", "steps": ["a step long enough to count as real, really " * 2] * 3,
                       "question": "q?", "answer": "a", "confident": True}
        thin_lesson = {"summary": "", "steps": [], "question": "", "answer": "", "confident": False}

        def fake(system, user):
            helped = _L["shots"]["intro"] in system
            if '"caveats"' in user:
                return json.dumps({"answer": "covered" if helped else "", "caveats": [], "confident": helped})
            return json.dumps(good_lesson if helped else thin_lesson)

        out = bench_eval.run(fake, bench, name="fake")
        n = len(bench["cases"])
        self.assertEqual(out["modes"]["without"], {"passed": 0, "cases": n})
        self.assertEqual(out["modes"]["with"], {"passed": n, "cases": n})  # every case finds a seed example
        self.assertIn("fake", bench_eval.render(out))

    def test_dry_run_calls_no_model(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(bench_eval.main([]), 0)
        self.assertIn("Dry run", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
