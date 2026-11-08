"""tutor/: ingest against a fixture vault and a fixture inbox, with a fake model.

Never touches the real vault, the real memory/ folder, Ollama or the
network: every run gets a temp vault, a temp state folder and a fake
`call(system, user) -> str`. The PDF, PPTX and DOCX are built here.

    .venv\\Scripts\\python.exe -m unittest discover -s tutor -p "test_*.py" -v
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import conf, extract, ingest, model, notes, topics  # noqa: E402
from tutor import __main__ as cli  # noqa: E402
from ollama import ResponseError  # noqa: E402

MODULES = os.path.join("Atlas", "Academic", "SEM 1 Modules")
SS = "5CCYB020 Signals and Systems"
EM = "5CCYB010 Electromagnetism"

PPTX_SLIDE = ('<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
              'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree>'
              '{body}</p:spTree></p:cSld></p:sld>')
PPTX_PARA = '<p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>'
DOCX_DOC = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            '{body}</w:body></w:document>')
DOCX_PARA = '<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'


def make_pptx(path, slides):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        for n, paras in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{n}.xml",
                       PPTX_SLIDE.format(body="".join(PPTX_PARA.format(text=p) for p in paras)))


def make_docx(path, paras):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", DOCX_DOC.format(body="".join(DOCX_PARA.format(text=p) for p in paras)))


def make_pdf(path, lines):
    """A one-page PDF with real text, through the installed PyMuPDF."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line)
        y += 16
    doc.save(path)
    doc.close()


def fake_model(log):
    """A model that reads the chunk it is given: topics named after what
    the text mentions, so the tests can see which chunk produced what."""
    def call(system, user):
        log.append(user)
        part = user.split("This is part ")[1].split(" of ")[0]
        topics_ = []
        if "Laplace" in user:
            topics_.append({"name": "Laplace transform", "prerequisites": ["Fourier transform"]})
        if "Fourier" in user:
            topics_.append({"name": "Fourier transform", "prerequisites": ["Complex numbers"]})
        return "Here you go:\n```json\n" + json.dumps({
            "outcomes": [f"Outcome from part {part}", "State the transform pair"],
            "methods": [{"name": "Transform definition", "equation": "$X(s)=\\int_0^\\infty x(t)e^{-st}dt$",
                         "use": "to move to the s-domain"}],
            "examples": [{"problem": "Transform a step", "steps": ["Write the integral", "Evaluate it"]}],
            "check": ["What is the region of convergence?"],
            "topics": topics_,
        }) + "\n```"
    return call


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="denisse-tutor-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.vault = os.path.join(self.tmp, "vault")
        self.state = os.path.join(self.tmp, "state")
        self.shared = os.path.join(self.tmp, "downloads", "KEATS")
        self.paths = conf.Paths(vault=self.vault, state=self.state, shared_inboxes=[self.shared])
        self.ss = os.path.join(self.vault, MODULES, SS)
        self.inbox = os.path.join(self.ss, "KEATS inbox")
        self.today = dt.date.today()
        # The vault: a root, a hub, a week note and a coursework note -- all with the contract's frontmatter.
        self.note(os.path.join(MODULES, "SEM 1 Modules.md"),
                  "---\nname: sem-1-modules\ndescription: hub\nupdated: 2026-11-01\ntags: [module]\n---\n"
                  "- [[Signals and Systems notes]]\n- [[Signals and Systems Week 4]]\n- [[Coursework]]\n")
        self.note(os.path.join(MODULES, SS, "Signals and Systems notes.md"),
                  "---\nname: signals-and-systems\ndescription: module hub\nupdated: 2026-11-01\ntags: [module]\n---\n"
                  "# Signals\n")
        self.note(os.path.join(MODULES, SS, "Week 4", "Signals and Systems Week 4.md"),
                  "---\nname: signals-and-systems-week-4\ndescription: week 4\nweek: 4\ntopic: Fourier\n"
                  "updated: 2026-11-01\ntags: [module]\nstatus: scaffolded\n---\n# Week 4\n")
        self.note(os.path.join(MODULES, SS, "Coursework", "Coursework.md"),
                  "---\nname: signals-coursework\ndescription: \"5CCYB020 coursework (30%)\"\nupdated: 2026-11-01\n"
                  "tags: [module]\n---\n> [!important] Due **Tuesday 24 November 2026, 16:00** on KEATS.\n\n"
                  "| Q | Topic | Marks | Revise from |\n|---|---|---|---|\n"
                  "| **1** | Signals & the Dirac delta — sketch | 14 | Wk 1 lecture |\n"
                  "| **2** | Laplace transform properties — table | 16 | Wk 5 lecture |\n\n"
                  "## Progress\n\n- [ ] Q1 sketches\n- [ ] Q2 table\n")
        os.makedirs(os.path.join(self.vault, MODULES, EM, "KEATS inbox"))
        # The inbox: a PDF in a Week folder, a PPTX named by lecture, a DOCX named W4, a txt with no week.
        os.makedirs(os.path.join(self.inbox, "Week 4"))
        self.pdf = os.path.join(self.inbox, "Week 4", "Lecture - Slides.pdf")
        make_pdf(self.pdf, ["The Fourier transform of a signal", "X(w) = integral of x(t) e^{-jwt} dt",
                            "Parseval: energy is preserved."])
        self.pptx = os.path.join(self.inbox, "Lecture 5 Laplace.pptx")
        make_pptx(self.pptx, [["The Laplace transform", "Region of convergence"], ["Worked example: unit step"]])
        self.docx = os.path.join(self.inbox, "W4 tutorial sheet.docx")
        make_docx(self.docx, ["Tutorial week 4: Fourier transform questions.", "Q1. Find the transform of a top hat."])
        self.txt = os.path.join(self.inbox, "reading list.txt")
        with open(self.txt, "w", encoding="utf-8") as f:
            f.write("Some general reading for the module, with no lecture number anywhere in it at all.\n")
        self.mp3 = os.path.join(self.inbox, "Week 4 recording.mp3")
        with open(self.mp3, "wb") as f:
            f.write(b"ID3 not really audio" * 10)
        # The shared inbox outside the vault: one placed by its content, one that can't be placed.
        os.makedirs(self.shared)
        self.shared_md = os.path.join(self.shared, "notes from a friend.md")
        with open(self.shared_md, "w", encoding="utf-8") as f:
            f.write("Signals and Systems week 6 handout.\n\nThe z-transform and its region of convergence, "
                    "with the Laplace transform as the starting point.\n")
        self.mystery = os.path.join(self.shared, "mystery.md")
        with open(self.mystery, "w", encoding="utf-8") as f:
            f.write("A page about nothing in particular, long enough to be read but naming no module and no week.\n")

    def note(self, rel, text):
        p = os.path.join(self.vault, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def run_ingest(self, log=None, **kw):
        log = [] if log is None else log
        out = io.StringIO()
        result = ingest.run(self.paths, call=fake_model(log), say=lambda s: out.write(s + "\n"),
                            today=self.today, **kw)
        return result, log, out.getvalue()

    def contract(self):
        """scripts/vault_contract.py on the fixture vault: (exit code, output).
        The checker's vault is a module-level constant with no override, so
        its VAULT assignment is swapped in the AST before running every one
        of its checks -- the same trick atrium/test_import_chats.py and
        scripts/vault_index_fix.py use."""
        command = """import ast, pathlib, sys
tree = ast.parse(pathlib.Path(sys.argv[2]).read_text(encoding='utf-8'))
for statement in tree.body:
    if isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VAULT' for t in statement.targets):
        statement.value = ast.Constant(sys.argv[1])
exec(compile(ast.fix_missing_locations(tree), sys.argv[2], 'exec'))
"""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        p = subprocess.run([sys.executable, "-c", command, self.vault, os.path.join(REPO, "scripts", "vault_contract.py")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=REPO)
        return p.returncode, p.stdout + p.stderr


class ExtractTests(Fixture):
    def test_pdf_pptx_docx_text(self):
        text, why = extract.read(self.pdf)
        self.assertEqual(why, "")
        self.assertIn("Fourier transform", text)
        text, why = extract.read(self.pptx)
        self.assertEqual(why, "")
        self.assertIn("--- slide 1 ---", text)
        self.assertIn("Region of convergence", text)
        self.assertIn("Worked example: unit step", text)
        text, why = extract.read(self.docx)
        self.assertEqual(why, "")
        self.assertIn("top hat", text)
        self.assertEqual(extract.read(self.txt)[1], "")

    def test_audio_off_and_unknown_type(self):
        self.assertEqual(extract.read(self.mp3)[1], conf.CONFIG["match"]["unmatched_reasons"]["audio_off"])
        self.assertEqual(extract.read(os.path.join(self.tmp, "x.xyz"))[1],
                         conf.CONFIG["match"]["unmatched_reasons"]["unknown_type"])


class MatchTests(Fixture):
    def test_week_from_folder_name_and_page(self):
        self.assertEqual(ingest.week_in("Week 4", folder=True), 4)
        self.assertEqual(ingest.week_in("Lecture 5 Laplace"), 5)
        self.assertEqual(ingest.week_in("W4 tutorial sheet"), 4)
        self.assertEqual(ingest.week_in("Signals and Systems week 6 handout."), 6)
        self.assertIsNone(ingest.week_in("reading list"))

    def test_module_by_code_name_or_words(self):
        mods = conf.modules(self.paths)
        self.assertEqual([m.title for m in mods], ["Electromagnetism", "Signals and Systems"])
        self.assertEqual(ingest.module_in("5CCYB020 week 6 handout", mods).title, "Signals and Systems")
        self.assertEqual(conf.find_module("signals", mods).title, "Signals and Systems")
        self.assertEqual(conf.find_module("Signals and Systems", mods).title, "Signals and Systems")
        # Part 3: the short forms he says -- the start of a one-word title, the initials of a longer one.
        self.assertEqual(conf.find_module("em", mods).title, "Electromagnetism")
        self.assertEqual(conf.find_module("EM", mods).title, "Electromagnetism")
        self.assertEqual(conf.find_module("ss", mods).title, "Signals and Systems")
        self.assertEqual(conf.find_module("comp", mods), None)  # not a word, an initial or a one-word start
        self.assertEqual(conf.find_module("e", mods), None)  # too short to be a short form
        self.assertEqual(conf.find_module("", mods), None)
        self.assertEqual(conf.split_folder("5CCYB070 Computational Methods (26~27 SY)"),
                         ("5CCYB070", "Computational Methods"))


class IngestTests(Fixture):
    def test_end_to_end_then_dedupe_then_change(self):
        result, log, said = self.run_ingest()
        # Three lectures: Week 4 (pdf + docx), Week 5 (pptx), Week 6 (shared inbox, placed by its page).
        self.assertEqual(sorted(os.path.basename(p) for p in result["notes"]),
                         ["Signals and Systems Week 4 tutor.md", "Signals and Systems Week 5 tutor.md",
                          "Signals and Systems Week 6 tutor.md"])
        self.assertEqual(result["failed"], [])
        reasons = {os.path.basename(u["path"]): u["why"] for u in result["unmatched"]}
        self.assertEqual(set(reasons), {"reading list.txt", "Week 4 recording.mp3", "mystery.md"})
        self.assertIn("no week", reasons["reading list.txt"])
        self.assertIn("audio", reasons["Week 4 recording.mp3"])
        self.assertIn("no module", reasons["mystery.md"])
        note4 = self.read(os.path.join(self.ss, "Week 4", "Signals and Systems Week 4 tutor.md"))
        self.assertTrue(note4.startswith("---\nname: signals-and-systems-week-4-tutor\n"))
        for heading in ("## Learning outcomes", "## Key methods and equations", "## Worked examples",
                        "## Check yourself", "## Topics", "## Sources"):
            self.assertIn(heading, note4)
        self.assertIn("$X(s)=", note4)
        self.assertIn("[[Signals and Systems Week 4|Week 4 note]]", note4)
        self.assertIn("[[Signals and Systems notes|Module hub]]", note4)
        self.assertIn("[[Atlas/Academic/SEM 1 Modules/5CCYB020 Signals and Systems/KEATS inbox/Week 4/"
                      "Lecture - Slides.pdf|Lecture - Slides.pdf]]", note4)
        self.assertIn("- [ ] What is the region of convergence?", note4)
        note6 = self.read(os.path.join(self.ss, "Week 6", "Signals and Systems Week 6 tutor.md"))
        self.assertIn("`notes from a friend.md` (outside the vault)", note6)
        self.assertFalse(os.path.exists(os.path.join(self.ss, "Week 6", "Signals and Systems Week 6.md")))
        self.assertNotIn("Week 6 note]]", note6)  # no week note to link to
        # Files stay where they were; the week note he has is untouched.
        self.assertTrue(os.path.exists(self.pdf) and os.path.exists(self.shared_md))
        self.assertIn("status: scaffolded", self.read(os.path.join(self.ss, "Week 4", "Signals and Systems Week 4.md")))
        # The topic map and the index.
        tm = json.loads(self.read(self.paths.topics_file("signals-and-systems")))
        by_name = {t["name"]: t for t in tm["topics"]}
        self.assertEqual(by_name["Fourier transform"]["lectures"], [4])
        self.assertEqual(by_name["Laplace transform"]["lectures"], [5, 6])
        self.assertEqual(by_name["Laplace transform"]["prerequisites"], ["Fourier transform"])
        self.assertEqual(by_name["Laplace transform"]["coursework"], ["Q2 table"])
        self.assertEqual(by_name["Fourier transform"]["coursework"], [])
        index = self.read(os.path.join(self.ss, "Signals and Systems tutor.md"))
        self.assertIn("[[Signals and Systems Week 5 tutor|Week 5]]", index)
        self.assertIn("**Laplace transform** — Week 5, 6; assumes Fourier transform; coursework Q2 table", index)
        # State is under the state folder it was given.
        self.assertTrue(os.path.exists(self.paths.state_file()))
        # The vault contract passes on what was written.
        code, out = self.contract()
        self.assertEqual(code, 0, out)
        self.assertIn("errors   : 0", out)

        # Second run: nothing is read or extracted again.
        result2, log2, said2 = self.run_ingest()
        self.assertEqual(log2, [])
        self.assertEqual(result2["notes"], [])
        self.assertEqual(len(result2["unchanged"]), 3)
        self.assertIn("known        Lecture 5 Laplace.pptx", said2)

        # A changed source regenerates only its week.
        make_pptx(self.pptx, [["The Laplace transform, revised", "Region of convergence"], ["New slide"]])
        result3, log3, _ = self.run_ingest()
        self.assertEqual([os.path.basename(p) for p in result3["notes"]], ["Signals and Systems Week 5 tutor.md"])
        self.assertEqual(len(log3), 1)
        self.assertEqual(len(result3["unchanged"]), 2)

    def test_never_overwrites_what_he_wrote(self):
        self.run_ingest()
        path = os.path.join(self.ss, "Week 5", "Signals and Systems Week 5 tutor.md")
        his = self.read(path) + "\n## My own notes\n\nKeep this.\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(his)
        make_pptx(self.pptx, [["The Laplace transform, second edition"]])
        result, _, said = self.run_ingest()
        self.assertEqual(self.read(path), his)
        copy = os.path.join(self.ss, "Week 5", "Signals and Systems Week 5 tutor (tutor update).md")
        self.assertEqual(result["notes"], [copy])
        self.assertTrue(self.read(copy).startswith("---\nname: signals-and-systems-week-5-tutor-update\n"))
        self.assertIn("kept yours", said)
        index = self.read(os.path.join(self.ss, "Signals and Systems tutor.md"))
        self.assertIn("[[Signals and Systems Week 5 tutor (tutor update)|", index)
        code, out = self.contract()
        self.assertEqual(code, 0, out)

    def test_dry_run_reads_nothing_into_the_vault(self):
        result, log, said = self.run_ingest(apply=False)
        self.assertEqual(log, [])
        self.assertEqual(result["notes"], [])
        self.assertIn("would write", said)
        self.assertFalse(os.path.exists(os.path.join(self.ss, "Week 4", "Signals and Systems Week 4 tutor.md")))
        self.assertFalse(os.path.exists(self.paths.state_file()))

    def test_model_failure_writes_no_note(self):
        out = io.StringIO()
        result = ingest.run(self.paths, call=lambda s, u: "I'd rather not.", say=lambda s: out.write(s + "\n"),
                            today=self.today)
        self.assertEqual(result["notes"], [])
        self.assertEqual(len(result["failed"]), 3)
        self.assertFalse(os.path.exists(os.path.join(self.ss, "Week 4", "Signals and Systems Week 4 tutor.md")))
        self.assertIn("FAILED", out.getvalue())
        # A failed week is tried again next time: nothing was recorded as done.
        result2, log2, _ = self.run_ingest()
        self.assertEqual(len(result2["notes"]), 3)

    def test_one_weeks_model_error_does_not_stop_the_run(self):
        """7-8 Nov 2026, live: Ollama's ResponseError on one week crashed the
        whole ingest and every later week was skipped."""
        good = fake_model([])
        calls = []

        def call(system, user):
            calls.append(user)
            if "Parseval" in user:
                raise ResponseError("prediction aborted, token repeat limit reached", 500)
            return good(system, user)
        out = io.StringIO()
        result = ingest.run(self.paths, call=call, say=lambda s: out.write(s + "\n"), today=self.today)
        said = out.getvalue()
        week4 = os.path.join(self.ss, "Week 4", "Signals and Systems Week 4 tutor.md")
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0][:2], (conf.find_module("Signals", conf.modules(self.paths)).slug, 4))
        self.assertEqual(len(result["notes"]), 2)  # weeks 5 and 6 still written
        self.assertFalse(os.path.exists(week4))
        self.assertIn("token repeat limit reached", said)
        self.assertIn("2 week note(s) written (0 incomplete)", said)
        self.assertIn("1 week(s) failed", said)
        self.assertIn("0 OCR page(s) used", said)
        # Asked 1 + chunk_retries times before giving up on that chunk.
        self.assertEqual(sum("Parseval" in u for u in calls), 1 + model._M["chunk_retries"])
        # Nothing recorded as done for week 4: the next run tries it again.
        result2, _log, _said = self.run_ingest()
        self.assertEqual(result2["notes"], [week4])

    def test_some_chunks_fail_writes_an_incomplete_note_retried_next_run(self):
        good = fake_model([])

        def call(system, user):
            if "Week 4" in user and "This is part 2 of" in user:
                raise ResponseError("prediction aborted", 500)
            return good(system, user)
        out = io.StringIO()
        result = ingest.run(self.paths, call=call, say=lambda s: out.write(s + "\n"), today=self.today,
                            num_ctx=2240)
        said = out.getvalue()
        self.assertEqual(result["failed"], [])
        self.assertEqual([(w, chunks) for _s, w, chunks in result["incomplete"]], [(4, [2])])
        week4 = os.path.join(self.ss, "Week 4", "Signals and Systems Week 4 tutor.md")
        self.assertIn(week4, result["notes"])
        body = self.read(week4)
        self.assertIn("[!warning] Incomplete: 1 of", body)
        self.assertIn("chunk 2 of", said)
        self.assertIn("(1 incomplete)", said)
        # The incomplete week is not "unchanged" next time: it is read again, now whole.
        result2, _log, _said = self.run_ingest(num_ctx=2240)
        self.assertEqual(result2["notes"], [week4])
        self.assertEqual(result2["incomplete"], [])
        self.assertNotIn("Incomplete:", self.read(week4))
        code, out = self.contract()
        self.assertEqual(code, 0, out)

    def test_no_inbox_is_quiet(self):
        shutil.rmtree(self.inbox)
        shutil.rmtree(self.shared)
        shutil.rmtree(os.path.join(self.vault, MODULES, EM, "KEATS inbox"))
        result, log, said = self.run_ingest()
        self.assertEqual(log, [])
        self.assertIn("no inbox folder found", said)
        self.assertEqual(result["notes"], [])


class ModelTests(unittest.TestCase):
    def test_chunks_fit_and_cover(self):
        text = "\n\n".join(f"Paragraph {i} " + "x" * 50 for i in range(40))
        pieces = model.chunks(text, 400)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= 400 for p in pieces))
        self.assertEqual("\n\n".join(pieces), text)
        self.assertEqual(model.chunks("short", 400), ["short"])
        long_line = "y" * 1000
        self.assertEqual("".join(model.chunks(long_line, 300)), long_line)

    def test_long_lecture_is_chunked_and_merged(self):
        log = []
        text = "Fourier " + "a" * 3000 + "\n\nLaplace " + "b" * 3000
        result, n = model.extract_lecture("Signals and Systems", "5CCYB020", 4, ["x.pdf"], text,
                                          call=fake_model(log), num_ctx=3000)
        self.assertGreater(n, 1)
        self.assertEqual(len(log), n)
        self.assertIn("This is part 1 of %d" % n, log[0])
        self.assertEqual([t["name"] for t in result["topics"]], ["Fourier transform", "Laplace transform"])
        self.assertEqual(result["outcomes"][:2], ["Outcome from part 1", "State the transform pair"])
        self.assertEqual(len(result["methods"]), 1)  # the same method from every chunk, once

    def test_retry_then_failure(self):
        replies = iter(["no json here", '{"outcomes": ["ok"]}'])
        log = []

        def flaky(system, user):
            log.append(user)
            return next(replies)
        result, _ = model.extract_lecture("M", "", 1, [], "text", call=flaky, num_ctx=3000)
        self.assertEqual(result["outcomes"], ["ok"])
        self.assertIn("not valid JSON", log[1])
        with self.assertRaises(model.ModelError):
            model.extract_lecture("M", "", 1, [], "text", call=lambda s, u: "nope", num_ctx=3000)

    def test_model_errors_are_per_chunk(self):
        text = "Fourier " + "a" * 3000 + "\n\nLaplace " + "b" * 3000
        good = fake_model([])

        def call(system, user):
            if "This is part 2 of" in user:
                raise ResponseError("prediction aborted, token repeat limit reached", 500)
            return good(system, user)
        failed, said = [], []
        result, n = model.extract_lecture("M", "", 4, [], text, call=call, num_ctx=3000, failed=failed,
                                          say=lambda k, parts, why: said.append((k, why)))
        self.assertGreater(n, 2)
        self.assertEqual([k for k, _ in failed], [2])
        self.assertIn("token repeat limit", failed[0][1])
        self.assertIn((1, None), said)
        self.assertIn("Outcome from part 1", result["outcomes"])
        self.assertNotIn("Outcome from part 2", result["outcomes"])

    def test_every_kind_of_model_error_is_caught(self):
        import httpx
        for exc in (ResponseError("boom", 500), TimeoutError("timed out"), ConnectionError("refused"),
                    httpx.ConnectError("no server"), httpx.ReadTimeout("slow"), model.ModelError("x")):
            def call(system, user, exc=exc):
                raise exc
            with self.assertRaises(model.ModelError):  # every chunk failed: the lecture's error
                model.extract_lecture("M", "", 1, [], "text", call=call, num_ctx=3000)

    def test_retry_recovers_with_configured_options(self):
        seen = []

        def call(system, user, options=None):
            seen.append(options)
            if len(seen) == 1:
                raise ResponseError("prediction aborted", 500)
            if len(seen) == 2:
                return "not json"
            return '{"outcomes": ["ok"]}'
        with unittest.mock.patch.dict(model._M, {"chunk_retries": 2,
                                                 "retry_options": [{"repeat_penalty": 9.0}, {"temperature": 0.9}]}):
            result, _ = model.extract_lecture("M", "", 1, [], "text", call=call, num_ctx=3000)
        self.assertEqual(result["outcomes"], ["ok"])
        self.assertEqual(seen, [None, {"repeat_penalty": 9.0}, {"temperature": 0.9}])
        self.assertEqual(model.retry_options(0), {})

    def test_parse_and_normalise_are_forgiving(self):
        self.assertEqual(model.parse("Sure! {\"outcomes\": [\"a\"]} done")["outcomes"], ["a"])
        self.assertIsNone(model.parse("[1, 2]"))
        self.assertIsNone(model.parse(""))
        out = model.normalise({"outcomes": ["a", "", 3], "methods": ["bare", {"name": "n", "equation": None}],
                               "examples": [{"problem": "p", "steps": "one step"}, "just text"],
                               "check": None, "topics": [{"name": "t", "prerequisites": "pre"}, "u"]})
        self.assertEqual(out["outcomes"], ["a", "3"])
        self.assertEqual(out["methods"][0], {"name": "bare", "equation": "", "use": ""})
        self.assertEqual(out["examples"][0]["steps"], ["one step"])
        self.assertEqual(out["examples"][1], {"problem": "just text", "steps": []})
        self.assertEqual(out["check"], [])
        self.assertEqual(out["topics"], [{"name": "t", "prerequisites": ["pre"]}, {"name": "u", "prerequisites": []}])

    def test_num_ctx_is_the_brains(self):
        with unittest.mock.patch.dict(os.environ, {conf.CONFIG["paths"]["context_env"]: "4096"}):
            self.assertEqual(conf.num_ctx(), 4096)
        with unittest.mock.patch.dict(os.environ, {conf.CONFIG["paths"]["context_env"]: ""}):
            self.assertGreater(conf.num_ctx(), 0)


class CliTests(Fixture):
    def test_topics_prints_the_map(self):
        self.run_ingest()
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = cli.main(["topics", "signals", "--vault", self.vault, "--state", self.state])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["module"], "Signals and Systems")
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = cli.main(["topics", "electromagnetism", "--vault", self.vault, "--state", self.state])
        self.assertEqual(rc, 1)
        self.assertIn("no topic map", out.getvalue())
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = cli.main(["topics", "basket weaving", "--vault", self.vault, "--state", self.state])
        self.assertEqual(rc, 1)
        self.assertIn("Electromagnetism, Signals and Systems", out.getvalue())


class NoLiteralsTests(unittest.TestCase):
    """Rule 0: no vault path, module name, model name or week-folder literal
    in the code's string constants (docstrings and comments may explain)."""

    def test_code_names_no_module_path_or_model(self):
        import ast
        for fn in os.listdir(HERE):
            if not fn.endswith(".py") or fn.startswith("test_"):
                continue
            with open(os.path.join(HERE, fn), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            docstrings = {node.body[0].value for node in ast.walk(tree)
                          if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
                          and node.body and isinstance(node.body[0], ast.Expr)
                          and isinstance(node.body[0].value, ast.Constant)}
            literals = [node.value for node in ast.walk(tree)
                        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings]
            for bad in ("Atlas", "SEM 1", "Signals", "Electromagnetism", "llama", "KEATS", "OneDrive",
                        "C:\\", "memory", "Week "):
                self.assertFalse([s for s in literals if bad in s], (fn, bad))


if __name__ == "__main__":
    unittest.main()
