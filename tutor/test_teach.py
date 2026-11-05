"""tutor/ part 3: explaining, quizzes, exam practice, marking, and the chat
tools -- fixture vault, fake models, a fake delegate.

Never touches the real vault, memory/, Ollama, Claude or the network: the
vault and cards come from test_don.DonFixture, every engine is a function
(chat.CALLS), delegate.py is a stand-in module, and the learner store is in
the fixture vault.

    .venv\\Scripts\\python.exe -m unittest discover -s tutor -p "test_*.py" -v
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import chat, conf, don, exam, teach  # noqa: E402
from tutor import __main__ as cli  # noqa: E402
from tutor.test_don import DonFixture, fake_cards  # noqa: E402
from tutor.test_tutor import MODULES, SS  # noqa: E402

CFG = conf.CONFIG
NAME = conf.tutor_name()


# -- fakes -------------------------------------------------------------------------

def fake_engine(log, tag="local", thin=False, no_json=False):
    """One function for every prompt the tutor sends: it reads which JSON
    shape is asked for and answers in it, naming what it was given."""
    def call(system, user):
        log.append((tag, user))
        if no_json:
            return "I would rather not."
        if '"awarded"' in user:  # marking an exam answer
            return json.dumps({"marks": 14, "total": 20,
                               "steps": [{"step": "definition", "awarded": 8, "of": 8, "comment": "Right."},
                                         {"step": "step transform", "awarded": 6, "of": 12, "comment": "Missed the ROC."}],
                               "feedback": f"{tag}: revise the region of convergence."})
        if '"steps"' in user:  # a lesson
            topic = user.split("Topic: ", 1)[1].split(".", 1)[0]
            if thin:
                return json.dumps({"summary": f"{topic}, thinly", "steps": ["Too short."], "question": "",
                                   "answer": "", "confident": False})
            seen = "Laplace" if "Laplace" in user.split("MATERIAL:", 1)[1] else "Fourier"
            return json.dumps({"summary": f"{tag} summary of {topic} (material mentions {seen})",
                               "steps": [f"Step one of {topic}: start from the definition in the notes.",
                                         f"Step two of {topic}: apply it to the unit step, as the worked example does.",
                                         f"Step three of {topic}: check the region of convergence."],
                               "question": f"What is the {topic} of a unit step?", "answer": "1/s", "confident": True})
        if '"mark_scheme"' in user:  # an exam question
            return json.dumps({"title": f"{tag} problem", "question": "(a) Define the transform. (b) Apply it to a step.",
                               "marks": 20, "mark_scheme": [{"step": "definition", "marks": 8},
                                                            {"step": "step transform", "marks": 12}]})
        if '"results"' in user:  # marking a quiz
            n = len(re.findall(r"^\d+\. Q:", user, re.M))
            return json.dumps({"results": [{"n": i, "correct": i % 2 == 1, "comment": f"q{i}"} for i in range(1, n)]})
        return "{}"
    return call


class FakeDelegate:
    """delegate.py as the tutor sees it: run() answers like the fake engine,
    on_done collects what the tray would be shown."""

    def __init__(self, log):
        self.DRY_RUN = False
        self.shown = []
        self.on_done = self.shown.append
        self._call = fake_engine(log, "claude")
        self.runs = []

    def run(self, task, context=None, engines=None):
        self.runs.append(engines)
        return True, self._call("", task), "claude", []


# -- the fixture -------------------------------------------------------------------------

class TeachFixture(DonFixture):
    def setUp(self):
        super().setUp()
        self.log = []
        self.delegate = FakeDelegate(self.log)
        patcher = unittest.mock.patch.dict("sys.modules", {"delegate": self.delegate})
        patcher.start()
        self.addCleanup(patcher.stop)
        chat.PATHS, chat.BACKGROUND, chat.CORE = self.paths, False, None
        chat.CALLS = {"local": fake_engine(self.log), "claude": fake_engine(self.log, "claude")}
        self.addCleanup(self._reset)
        self.run_ingest()

    @staticmethod
    def _reset():
        chat.PATHS, chat.CALLS, chat.BACKGROUND, chat.CORE = None, None, True, None

    def with_cards(self):
        self.make_cards()

    def anki_stats(self, weak=("Laplace transform",)):
        stats = {"module": "Signals and Systems", "code": "5CCYB020", "synced": "2026-11-05", "cards": 6, "reviewed": 4,
                 "topics": [{"topic": "Laplace transform", "cards": 3, "reviewed": 2, "reviews": 6, "lapses": 3,
                             "ease": 190, "due": 1, "weak": "Laplace transform" in weak, "score": 2.0, "weeks": [5]},
                            {"topic": "Fourier transform", "cards": 3, "reviewed": 2, "reviews": 4, "lapses": 0,
                             "ease": 260, "due": 0, "weak": False, "score": 0.0, "weeks": [4]}],
                 "weak": list(weak)}
        don.save_json(self.paths, CFG["don"]["anki_file"].format(slug="signals-and-systems"), stats)

    def lessons(self):
        return self.read(teach.lessons_path(self.paths, self.ss_module))


# -- explaining ---------------------------------------------------------------------------

class MaterialTests(TeachFixture):
    def test_material_is_topic_map_weeks_then_notes_that_name_it(self):
        found = teach.material(self.paths, self.ss_module, "Laplace transform")
        self.assertEqual(found["weeks"], [5, 6])
        self.assertIn("Signals and Systems Week 5 tutor", found["text"])
        self.assertIn("My own Laplace summary", found["text"])   # his note in the module folder
        self.assertIn("Laplace tricks", found["text"])           # his note elsewhere, tagged with the code
        self.assertNotIn("2026-11-02 study", found["text"])      # about Fourier, not Laplace
        self.assertTrue(any("Week 5 tutor" in link for link in found["links"]))
        self.assertTrue(all(link.startswith("[[") for link in found["links"]))
        self.assertEqual(teach.material(self.paths, self.ss_module, "quantum chromodynamics")["text"], "")

    def test_hard_topics_come_from_config(self):
        self.assertFalse(teach.is_hard("Laplace transform"))
        with unittest.mock.patch.dict(CFG["teach"], {"hard_topics": ["laplace"]}):
            self.assertTrue(teach.is_hard("Laplace transform"))
            self.assertFalse(teach.is_hard("Fourier transform"))

    def test_learner_lines_come_from_the_learner_model(self):
        from learner import core
        data = core.load()
        core.add_belief(data, "Wants a worked example before the theory", "format", "stated")
        core.add_belief(data, "Does best work in the morning", "schedule", "stated")
        core.save(data)
        lines = teach.learner_lines()
        self.assertIn("worked example", lines)
        self.assertNotIn("morning", lines)  # not one of teach.learner_areas
        with unittest.mock.patch.object(core, "load", side_effect=OSError("no store")):
            self.assertEqual(teach.learner_lines(), CFG["teach"]["learner_none"])


class ExplainTests(TeachFixture):
    def test_local_lesson_in_the_tutors_name_and_in_the_vault(self):
        out = chat.explain("Laplace transform", "signals")
        self.assertTrue(out.startswith(f"{NAME} here."), out)
        self.assertIn("Step one of Laplace transform", out)
        self.assertIn("Check question for him: What is the Laplace transform of a unit step?", out)
        self.assertIn("Model answer", out)
        self.assertEqual([tag for tag, _ in self.log], ["local"])
        self.assertIn("How he learns", self.log[0][1])
        # Recorded: the sidecar, the lessons note, the progress note, the hub.
        data = json.loads(self.read(self.paths.data(CFG["teach"]["lesson_file"].format(slug="signals-and-systems"))))
        self.assertEqual([l["topic"] for l in data["lessons"]], ["Laplace transform"])
        note = self.lessons()
        self.assertIn(f"# Signals and Systems: lessons by {NAME}", note)
        self.assertIn("## " + self.today.isoformat() + ": Laplace transform (local)", note)
        self.assertIn("[[Atlas/Academic/SEM 1 Modules/5CCYB020 Signals and Systems/Week 5/Signals and Systems Week 5 tutor|", note)
        progress = self.read(don.progress_path(self.paths, self.ss_module))
        self.assertIn("## Lessons and practice", progress)
        self.assertIn(f"1 topic(s) explained by {NAME}; last: Laplace transform", progress)
        self.assertIn("|lessons]]", self.read(don.hub_path(self.paths)))
        self.assertEqual(self.delegate.shown, [])
        code, report = self.contract()
        self.assertEqual(code, 0, report)

    def test_no_material_is_said_without_a_model_call(self):
        out = chat.explain("quantum chromodynamics", "signals")
        self.assertIn("nothing in the vault about 'quantum chromodynamics'", out)
        self.assertEqual(self.log, [])
        self.assertFalse(os.path.exists(teach.lessons_path(self.paths, self.ss_module)))

    def test_hard_topic_goes_to_claude_and_pops_up(self):
        with unittest.mock.patch.dict(CFG["teach"], {"hard_topics": ["laplace transform"]}):
            out = chat.explain("Laplace transform", "signals")
        self.assertIn("marked as a hard topic", out)
        self.assertIn("asked Claude", out)
        self.assertEqual([tag for tag, _ in self.log], ["claude"])  # the local model was never asked
        self.assertEqual(len(self.delegate.shown), 1)
        self.assertTrue(self.delegate.shown[0].startswith(f"{NAME}: Claude's explanation of Laplace transform"))
        self.assertIn("(claude)", self.lessons())

    def test_local_answer_failing_its_check_is_escalated_with_the_local_take_meanwhile(self):
        chat.CALLS["local"] = fake_engine(self.log, thin=True)
        out = chat.explain("Fourier transform", "signals")
        self.assertIn("didn't pass", out)
        self.assertIn("Meanwhile, the local model's take", out)
        self.assertEqual([tag for tag, _ in self.log], ["local", "claude"])
        self.assertEqual(len(self.delegate.shown), 1)
        self.assertIn("## " + self.today.isoformat() + ": Fourier transform (claude)", self.lessons())
        self.assertNotIn("(local)", self.lessons())  # the thin answer was not recorded

    def test_claude_through_delegate_run_when_no_call_is_injected(self):
        chat.CALLS = {"local": fake_engine(self.log, thin=True)}
        chat.explain("Fourier transform", "signals")
        self.assertEqual(self.delegate.runs, [CFG["cards"]["engines"]["claude"]["delegate_engines"]])
        self.assertEqual(len(self.delegate.shown), 1)

    def test_dry_run_hands_nothing_off(self):
        self.delegate.DRY_RUN = True
        chat.CALLS["local"] = fake_engine(self.log, thin=True)
        out = chat.explain("Fourier transform", "signals")
        self.assertIn("hand-offs are off", out)
        self.assertEqual([tag for tag, _ in self.log], ["local"])
        self.assertEqual(self.delegate.shown, [])

    def test_model_without_json_is_reported_not_invented(self):
        chat.CALLS["local"] = fake_engine(self.log, no_json=True)
        out = chat.explain("Laplace transform", "signals")
        self.assertIn("didn't give a usable explanation", out)
        self.assertFalse(os.path.exists(teach.lessons_path(self.paths, self.ss_module)))

    def test_module_is_worked_out_or_asked(self):
        self.assertIn("Step one of Laplace transform", chat.explain("Laplace transform"))  # only Signals maps it
        out = chat.explain("Laplace transform", "basket weaving")
        self.assertIn("don't know a module called 'basket weaving'", out)
        self.assertIn("Electromagnetism, Signals and Systems", out)
        self.assertEqual(len(self.log), 1)
        self.assertIn("nothing in the vault about 'Coulomb'", chat.explain("Coulomb", "EM"))  # the short form


# -- quizzes, weak topics ------------------------------------------------------------------

class QuizTests(TeachFixture):
    def test_no_cards_is_said(self):
        out = chat.quiz("signals")
        self.assertTrue(out.startswith(f"{NAME} here."))
        self.assertIn("No cards for Signals and Systems yet", out)
        self.assertIn("python -m tutor cards", out)

    def test_quiz_weak_first_then_marked(self):
        self.with_cards()
        self.anki_stats()
        out = chat.quiz("signals")
        n = CFG["quiz"]["questions"]
        self.assertIn(f"{n} question(s) on Signals and Systems", out)
        questions = re.findall(r"^(\d+)\. \[([^\]]+)\] ", out, re.M)
        self.assertEqual(len(questions), n)
        weak_first = [t for _, t in questions]
        self.assertEqual(weak_first[0], "Laplace transform")
        self.assertEqual(exam.open_item(self.paths, self.ss_module)["kind"], exam.KIND_QUIZ)
        # Narrowed and counted.
        out2 = chat.quiz("signals", "Fourier", 2)
        self.assertEqual(re.findall(r"^\d+\. \[Fourier transform\]", out2, re.M).__len__(), 2)
        # His answers, marked against the cards by the local model.
        out3 = chat.mark_answer("1. 1/s\n2. no idea")
        self.assertTrue(out3.startswith(f"{NAME} here."))
        self.assertIn("1/1 right", out3)
        self.assertIn("1. right.", out3)
        self.assertIn("2. not answered", out3)
        self.assertIn("card says:", out3)
        self.assertEqual([tag for tag, _ in self.log if "His answers" in _], ["local"])
        self.assertIsNone(exam.open_item(self.paths, self.ss_module))
        self.assertIn("## Quizzes", self.read(exam.note_path(self.paths, self.ss_module)))
        self.assertIn("1 quiz(zes): last 1/1 right (2 set", self.read(don.progress_path(self.paths, self.ss_module)))
        self.assertIn("no open question", chat.mark_answer("1. 1/s", "signals"))

    def test_round_offer_only_with_cards_and_reaches_the_clock(self):
        import study_mode
        self.assertEqual(chat.round_offer(1), "")
        self.with_cards()
        offer = chat.round_offer(2)
        self.assertIn(NAME, offer)
        self.assertIn(str(CFG["quiz"]["questions"]), offer)
        with unittest.mock.patch.object(study_mode, "_chime", lambda: None), \
                unittest.mock.patch.object(study_mode, "_CURRENT", None), \
                unittest.mock.patch.object(study_mode, "_round_end", None):
            study_mode.set_round_end(chat.round_offer)

            class NoTkRoot:  # after() recorded, never run; the badge fails to build and is cosmetic
                def after(self, ms, fn):
                    return 1

                def after_cancel(self, job):
                    pass
            mode = study_mode.StudyMode(NoTkRoot(), lambda *a, **k: None)
            mode.start()
            self.assertEqual(mode.offer, "")
            self.assertNotIn(NAME, mode.describe())
            mode._enter("rest")
            self.assertIn(NAME, mode.describe())
            self.assertIn(NAME, study_mode.context_line())
            mode._enter("study")
            self.assertNotIn(NAME, mode.describe())
            mode.stop()
            study_mode.set_round_end(lambda n: 1 / 0)
            mode._enter("rest")
            self.assertEqual(mode.offer, "")  # a failing hook never breaks the clock


class WeakTests(TeachFixture):
    def test_no_stats_is_said(self):
        out = chat.weak_topics("signals")
        self.assertIn("No Anki review stats for Signals and Systems yet", out)
        self.assertIn("anki-sync", out)

    def test_weak_topics_from_the_stats_and_the_learner(self):
        self.anki_stats()
        from learner import core
        data = core.load()
        core.add_belief(data, "Finds Laplace transform (Signals and Systems) hard to keep: it keeps lapsing in Anki",
                        CFG["learner"]["area"], "measured", "test")
        core.save(data)
        out = chat.weak_topics("Signals and Systems")
        self.assertTrue(out.startswith(f"{NAME} here. Weak in Signals and Systems"))
        self.assertIn("Laplace transform (3 lapse(s) over 2 reviewed card(s); revise week(s) 5)", out)
        self.assertNotIn("Fourier", out)
        self.assertIn("learner model also says: Finds Laplace transform", out)
        self.anki_stats(weak=())
        self.assertIn("Nothing measured as weak", chat.weak_topics("signals"))


# -- exam practice ----------------------------------------------------------------------------

class ExamTests(TeachFixture):
    def test_question_in_coursework_style_then_marked_by_claude(self):
        out = chat.exam_question("signals", "Laplace transform")
        self.assertTrue(out.startswith(f"{NAME} here. Exam-style question on Signals and Systems (Laplace transform)"))
        self.assertIn("in the style of his Coursework", out)
        self.assertIn("(a) Define the transform.", out)
        self.assertNotIn("mark_scheme", out)
        prompt = self.log[-1][1]
        self.assertIn("Due **Tuesday 24 November 2026", prompt)   # the coursework note, as style
        self.assertIn("Laplace", prompt.split("MATERIAL:", 1)[1])
        item = exam.open_item(self.paths, self.ss_module)
        self.assertEqual((item["kind"], item["marks"]), (exam.KIND_EXAM, 20))
        self.assertEqual(sum(s["marks"] for s in item["mark_scheme"]), 20)
        self.assertIn("### Open: local problem (20 marks", self.read(exam.note_path(self.paths, self.ss_module)))
        # Marked against the scheme by the claude engine (injected here; delegate.run otherwise).
        out2 = chat.mark_answer("(a) X(s) = int x e^-st dt  (b) 1/s")
        self.assertTrue(out2.startswith(f"{NAME} here. 14/20 on 'local problem'"))
        self.assertIn("- step transform: 6/12 -- Missed the ROC.", out2)
        self.assertIn("Feedback: claude: revise", out2)
        self.assertEqual(self.log[-1][0], "claude")
        self.assertIsNone(exam.open_item(self.paths, self.ss_module))
        note = self.read(exam.note_path(self.paths, self.ss_module))
        self.assertIn("### " + self.today.isoformat() + ": local problem -- 14/20", note)
        self.assertIn("> [!tip] claude: revise", note)
        self.assertNotIn("### Open:", note)
        progress = self.read(don.progress_path(self.paths, self.ss_module))
        self.assertIn("1 exam-style question(s) marked, 70% on average; last: 14/20 on local problem", progress)
        code, report = self.contract()
        self.assertEqual(code, 0, report)

    def test_marking_goes_to_delegate_in_the_background_and_pops_up(self):
        chat.exam_question("signals")
        self.assertIn("Week 6", self.log[-1][1].split("MATERIAL:", 1)[1])  # no topic: the latest tutor note
        chat.CALLS = {"local": fake_engine(self.log)}  # no claude call injected: delegate.run it is
        out = chat.mark_answer("my answer", "signals")
        self.assertIn("sent his answer to Claude for marking", out)
        self.assertEqual(len(self.delegate.runs), 1)
        self.assertEqual(len(self.delegate.shown), 1)
        self.assertTrue(self.delegate.shown[0].startswith(f"{NAME} here. 14/20"))
        self.assertIsNone(exam.open_item(self.paths, self.ss_module))

    def test_no_coursework_means_the_notes_set_the_style(self):
        em = next(m for m in conf.modules(self.paths) if m.code == "5CCYB010")
        self.assertEqual(exam.style_text(em), ("", []))
        out = chat.exam_question("EM")
        self.assertIn("no coursework or past paper", out)

    def test_engine_without_json_is_reported(self):
        chat.CALLS["local"] = fake_engine(self.log, no_json=True)
        self.assertIn("didn't give a usable exam question", chat.exam_question("signals"))
        self.assertIsNone(exam.open_item(self.paths, self.ss_module))


# -- Denisse's side: registry, policy, router, the reply -------------------------------------------

class RegistrationTests(TeachFixture):
    TOOLS = ("tutor_explain", "tutor_quiz", "tutor_weak_topics", "tutor_exam_question", "tutor_mark_answer")

    def test_registered_described_and_policed(self):
        import brain
        import tool_policy
        import tool_router
        import tools
        defs = {d["function"]["name"]: d["function"] for d in brain.TOOL_DEFINITIONS}
        for name in self.TOOLS:
            self.assertIn(name, tools.TOOL_FUNCTIONS)
            self.assertIn(name, defs)
            self.assertNotIn(NAME, defs[name]["description"])  # the name is tutor.json's, not brain.json's
            meta = tool_policy.tool(name)
            self.assertIn(meta["side_effect"], ("read", "write"))
            # The reply must carry the tutor's name; the policy's pattern and stand-in must be that name.
            self.assertTrue(re.search(meta["reply_must_mention"], f"{NAME} here."), name)
            self.assertFalse(re.search(meta["reply_must_mention"], "Here are five questions."), name)
            self.assertEqual(meta["fallback_reply"], CFG["chat"]["intro"].format(tutor=NAME), name)
        self.assertEqual(tool_policy.side_effect("tutor_mark_answer"), "write")
        self.assertTrue(tool_policy.allowed_unoffered("tutor_explain"))
        for text in ("quiz me on signals", "explain convolution", "what's weak in EM?", "give me an exam question",
                     "here's my answer: 1/s", "teach me the laplace transform"):
            offered = {d["function"]["name"] for d in tool_router.select(brain.TOOL_DEFINITIONS,
                                                                           [{"role": "user", "content": text}])}
            self.assertTrue(set(self.TOOLS) <= offered, text)
        self.assertFalse(set(self.TOOLS) & {d["function"]["name"] for d in tool_router.select(
            brain.TOOL_DEFINITIONS, [{"role": "user", "content": "what's the weather in Penang?"}])})

    def test_tool_functions_reach_the_tutor(self):
        import tools
        self.with_cards()
        self.assertTrue(tools.TOOL_FUNCTIONS["tutor_quiz"](module="signals").startswith(f"{NAME} here."))
        self.assertTrue(tools.TOOL_FUNCTIONS["tutor_weak_topics"]("signals").startswith(f"{NAME} here."))
        self.assertIn(NAME, tools.tutor_round_offer(1))

    def test_a_reply_that_drops_the_tutor_gets_its_first_sentence(self):
        import brain

        def call(name, args=None):
            return {"message": {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}}

        def run_turn(prompt, responses):
            bot = brain.Brain.__new__(brain.Brain)
            it = iter(responses)
            isolated = {name: (lambda **kw: "Refused: isolated tool") for name in brain.tools.TOOL_FUNCTIONS}
            isolated["tutor_quiz"] = brain.tools.tutor_quiz
            with unittest.mock.patch.object(brain.ollama, "chat", side_effect=lambda **kw: next(it)), \
                    unittest.mock.patch.dict(brain.tools.TOOL_FUNCTIONS, isolated, clear=True):
                return bot.respond([{"role": "user", "content": prompt}])[0]
        self.with_cards()
        out = run_turn("quiz me on signals", [call("tutor_quiz", {"module": "signals"}),
                                              {"message": {"content": "Here are five questions for you."}}])
        self.assertTrue(out.startswith(f"{NAME} here. Here are five questions for you."), out)
        out = run_turn("quiz me on signals", [call("tutor_quiz", {"module": "signals"}),
                                              {"message": {"content": f"{NAME} has five questions for you: 1..."}}])
        self.assertTrue(out.startswith(f"{NAME} has five"), out)


class ParseTests(unittest.TestCase):
    """model.parse on what the local model really sent for an exam question
    (2026-11-05): LaTeX backslashes JSON can't take, and no final brace."""

    def test_latex_backslashes_and_a_missing_brace(self):
        from tutor import model
        reply = ('{"title": "Fourier", "question": "Period $T=2\\pi$, $c_k=\\frac{1}{T}\\int_T x(t)e^{-jk\\omega_0 t}dt$. '
                 '(a) Write the harmonics.", "marks": 20, "mark_scheme": [{"step": "harmonics", "marks": 4}]')
        data = model.parse(reply)
        self.assertIsNotNone(data)
        self.assertEqual(data["marks"], 20)
        self.assertIn("\\pi", data["question"])
        self.assertIn("\\frac{1}{T}", data["question"])
        self.assertEqual(data["mark_scheme"][0]["marks"], 4)
        latex = model.parse('{"s": "$\\theta \\neq \\beta$, $\\tau$, $\\nabla \\cdot E$\\nThen $\\rho$."}')["s"]
        self.assertEqual(latex, "$\\theta \\neq \\beta$, $\\tau$, $\\nabla \\cdot E$\nThen $\\rho$.")
        # Valid escapes and a quote inside a string are left alone; prose around it is still tolerated.
        self.assertEqual(model.parse('Sure:\n{"a": "line\\nbreak \\"q\\" {x}"} done'), {"a": 'line\nbreak "q" {x}'})
        self.assertIsNone(model.parse("no json here"))
        self.assertEqual(model.parse('{"a": [1, 2'), {"a": [1, 2]})  # a reply that stopped early is closed
        self.assertIsNone(model.parse('{"a": [1, 2'[:-5]))  # ...but not invented: '{"a' has no value to close


class CliTests(TeachFixture):
    def test_commands(self):
        self.with_cards()
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            for argv in (["explain", "signals", "Laplace", "transform"], ["quiz", "signals", "Fourier"],
                         ["mark", "signals", "1.", "1/s"], ["weak", "signals"], ["exam", "signals"],
                         ["mark", "signals", "an", "answer"]):
                rc = cli.main(argv + ["--vault", self.vault, "--state", self.state], calls=chat.CALLS)
                self.assertEqual(rc, 0, argv)
        said = out.getvalue()
        self.assertIn("Step one of Laplace transform", said)
        self.assertIn("[Fourier transform]", said)
        self.assertIn("right", said)
        self.assertIn("No Anki review stats", said)
        self.assertIn("(a) Define the transform.", said)
        self.assertIn("14/20", said)
        self.assertEqual(cli.main(["quiz", "basket weaving", "--vault", self.vault, "--state", self.state]), 0)


if __name__ == "__main__":
    unittest.main()
