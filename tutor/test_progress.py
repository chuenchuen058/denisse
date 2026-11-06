"""tutor/ part 4: the progress tracker -- fixture vault, fake model, fake AnkiConnect.

Never touches the real vault, memory/, Ollama, Anki or the network: the
fixture vault and inbox come from test_tutor.Fixture, the cards and the
review stats from test_don's fakes, and the learner store is pointed into
the fixture (DonFixture).

    .venv\\Scripts\\python.exe -m unittest discover -s tutor -p "test_*.py" -v
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import anki, conf, don, progress, sources  # noqa: E402
from tutor import __main__ as cli  # noqa: E402
from tutor.test_don import DonFixture, FakeAnki, Server, setUpModule, tearDownModule  # noqa: E402,F401
from tutor.test_tutor import SS, make_pdf  # noqa: E402

CFG = conf.CONFIG
PR = CFG["progress"]
DAY = dt.date(2026, 11, 10)  # 14 days before the fixture coursework's 24 Nov deadline


class ProgressFixture(DonFixture):
    def setUp(self):
        super().setUp()
        self.run_ingest()
        self.make_cards()
        self.fake = FakeAnki()
        self.server = Server(self.fake)
        self.addCleanup(self.server.close)
        self.client = anki.Connect(url=self.server.url, timeout=5)
        anki.run(self.paths, [self.ss_module], client=self.client, today=self.today, say=self.out)
        self.fake.review("Laplace transform", lapses=3, factor=1900)
        self.fake.review("Fourier transform", lapses=0, factor=2600, due=False)
        anki.run(self.paths, [self.ss_module], client=self.client, today=self.today, say=self.out)
        # Downloaded since the last ingest: placed by its name, not read.
        make_pdf(os.path.join(self.inbox, "Lecture 7 Filters.pdf"), ["Filters and Bode plots"])

    def progress(self, modules=None):
        return progress.run(self.paths, modules, today=DAY, say=self.out)

    def study_note(self):
        return self.read(progress.note_path(self.paths))


class RecordTests(ProgressFixture):
    def test_lectures_topics_mastery_weak_coursework_next(self):
        out = self.progress()
        rec = next(r for r in out["records"] if r["code"] == "5CCYB020")
        lec = rec["lectures"]
        self.assertEqual(lec["read"], [4, 5, 6])
        self.assertEqual(lec["unread"], [7])
        self.assertEqual(lec["new_files"], 1)
        self.assertEqual(lec["on_keats"], [4, 5, 6, 7])
        by = {t["topic"]: t for t in rec["topics"]}
        lap, fou = by["Laplace transform"], by["Fourier transform"]
        self.assertTrue(lap["weak"])
        self.assertFalse(fou["weak"])
        self.assertEqual(lap["mastery"], 0)  # three lapses a card: nothing kept
        self.assertEqual(fou["mastery"], 100)  # every card reviewed, none lapsed
        self.assertIn(5, lap["weeks"])
        self.assertEqual([w["topic"] for w in rec["weak"]], ["Laplace transform"])
        self.assertIn((5, "Signals and Systems Week 5 tutor"), [tuple(n) for n in rec["weak"][0]["notes"]])
        self.assertIn("Q2 table", rec["weak"][0]["coursework"])
        cw = rec["coursework"][0]
        self.assertEqual((cw["due"], cw["days"], cw["total"]), ("2026-11-24", 14, 2))
        self.assertEqual(cw["left"], ["Q1 sketches", "Q2 table"])
        kinds = [n["kind"] for n in rec["next"]]
        self.assertEqual(kinds[:3], ["ingest", "weak_coursework", "deadline"])
        self.assertIn("7", rec["next"][0]["text"])
        self.assertIn("Laplace transform", rec["next"][1]["text"])
        self.assertIn("[[Signals and Systems Week 5 tutor|Week 5]]", rec["next"][1]["text"])
        self.assertLessEqual(len(rec["next"]), PR["next"]["max"])
        # The order is data: swap it and the list follows.
        with unittest.mock.patch.dict(PR["next"], order=["deadline", "ingest"]):
            again = progress.study_next(self.ss_module, rec)
        self.assertEqual([n["kind"] for n in again], ["deadline", "ingest"])
        # A module with nothing at all (EM: an empty inbox) gets no record.
        self.assertEqual([r["code"] for r in out["records"]], ["5CCYB020"])

    def test_sidecar_dataset_and_hub_rows(self):
        self.progress()
        rec = json.loads(self.read(self.paths.data(PR["module_file"].format(slug="signals-and-systems"))))
        self.assertEqual(rec["module"], "Signals and Systems")
        rows = progress.hub_rows(self.vault)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["lectures_read"], row["lectures_on_keats"], row["lectures_unread"]), (3, 4, "7"))
        self.assertEqual(row["weak"], "Laplace transform")
        self.assertEqual((row["next_deadline"], row["days_left"], row["coursework_left"]), ("2026-11-24", 14, 2))
        self.assertTrue(row["next"])
        self.assertEqual(rows, json.loads(self.read(self.paths.data(PR["dataset_file"]))))

    def test_learner_model_fed_once(self):
        self.progress()
        self.progress()
        model = json.loads(self.read(os.path.join(self.vault, "Dev", "learner", "model.json")))
        current = [b for b in model["beliefs"] if b["status"] == "current"]
        self.assertEqual(len(current), 1)
        self.assertIn("Laplace transform", current[0]["claim"])


class NoteTests(ProgressFixture):
    def test_note_written_linked_and_contract_passes(self):
        out = self.progress()
        # The learner note (fed by the Anki stats) links its guide, which the real vault has.
        self.note(os.path.join("Atlas", "Concepts", "Learning", "How-Chuen-Learns.md"),
                  "---\nname: how-chuen-learns\ndescription: guide\nupdated: 2026-11-01\ntags: [learning]\n---\n"
                  "[[Learner-Model]]\n")
        path = progress.note_path(self.paths)
        self.assertEqual(os.path.relpath(path, self.vault).split(os.sep), PR["note"])
        self.assertIn(path, out["touched"])
        text = self.study_note()
        start = PR["block_start"].format(tutor_slug=conf.tutor_slug())
        end = PR["block_end"].format(tutor_slug=conf.tutor_slug())
        self.assertEqual(text.count(start), 1)
        self.assertEqual(text.count(end), 1)
        self.assertIn(f"updated: {DAY.isoformat()}", text)
        self.assertIn(conf.tutor_name(), text)
        self.assertIn("## Signals and Systems (5CCYB020)", text)
        self.assertIn("| Laplace transform |", text)
        self.assertIn("2026-11-24 (in 14 days)", text)
        self.assertIn("week(s) 7", text)
        # The hub links the overview, so it is no orphan.
        self.assertIn("[[Study Progress]]", self.read(don.hub_path(self.paths)))
        # Not a source of cards: it names no module in its frontmatter and links no hub.
        self.assertNotIn(os.path.basename(path), [os.path.basename(s.path)
                                                  for s in sources.collect(self.paths, self.ss_module)])
        progress.run(self.paths, today=self.today, say=self.out)  # dated today: the contract refuses a future date
        code, said = self.contract()
        self.assertEqual(code, 0, said)
        self.assertNotIn("Study Progress.md", said)  # no error and no warning about it

    def test_his_edits_outside_the_block_are_kept(self):
        self.progress()
        path = progress.note_path(self.paths)
        text = self.study_note()
        start = PR["block_start"].format(tutor_slug=conf.tutor_slug())
        end = PR["block_end"].format(tutor_slug=conf.tutor_slug())
        mine_top, mine_bottom = "My plan: Laplace before the coursework.\n", "\n## My notes\n\nAsk about Q2.\n"
        text = text.replace(start, mine_top + start) + mine_bottom
        text = text.replace("Laplace transform |", "EDITED INSIDE |", 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        later = DAY + dt.timedelta(days=1)
        progress.run(self.paths, today=later, say=self.out)
        again = self.study_note()
        self.assertIn(mine_top + start, again)
        self.assertTrue(again.endswith(mine_bottom))
        self.assertNotIn("EDITED INSIDE", again)  # inside the block is the tutor's
        self.assertIn(f"updated: {later.isoformat()}", again)
        self.assertIn("(in 13 days)", again)

    def test_his_note_without_markers_gets_the_block_appended(self):
        path = progress.note_path(self.paths)
        his = "---\nname: study-progress\ndescription: mine\nupdated: 2026-11-01\ntags: [academic]\n---\n\nMine.\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(his)
        self.progress()
        text = self.study_note()
        self.assertTrue(text.startswith(his.replace("2026-11-01", DAY.isoformat())))
        self.assertIn(PR["block_start"].format(tutor_slug=conf.tutor_slug()), text)

    def test_one_module_keeps_the_others(self):
        self.progress()
        rows = progress.hub_rows(self.vault)
        progress.run(self.paths, [self.ss_module], today=DAY, say=self.out)
        self.assertEqual(progress.hub_rows(self.vault), rows)

    def test_cli(self):
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = cli.main(["progress", "--vault", self.vault, "--state", self.state])
            self.assertEqual(rc, 0)
            rc = cli.main(["progress", "basket weaving", "--vault", self.vault, "--state", self.state])
            self.assertEqual(rc, 1)
        said = out.getvalue()
        self.assertIn("study note", said)
        self.assertIn("vault commit skipped", said)  # the fixture vault isn't a git repo
        self.assertTrue(os.path.isfile(progress.note_path(self.paths)))


class BriefTests(ProgressFixture):
    """study_brief.py reads the tutor's weak topics: they go first."""

    def setUp(self):
        super().setUp()
        import study_brief
        self.sb = study_brief
        patcher = unittest.mock.patch.object(study_brief, "VAULT", self.vault)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_progress_run_means_no_focus(self):
        self.assertEqual(self.sb.tutor_focus("signals"), {})
        self.assertEqual(self.sb.revise_steps({}), [])
        items = [{"text": "Q1"}, {"text": "Q2"}]
        self.assertEqual(self.sb.prioritise(items, {}), items)

    def test_weak_topics_lead_the_brief_and_the_plan(self):
        self.progress()
        focus = self.sb.tutor_focus("signals")
        self.assertEqual(focus["tutor"], conf.tutor_name())
        self.assertEqual([w["topic"] for w in focus["weak"]], ["Laplace transform"])
        # Coursework items: the one the weak topic feeds goes first.
        q1 = {"text": "Q1 sketches", "done": False, "weeks": [1]}
        q2 = {"text": "Q2 table", "done": False, "weeks": [5]}
        self.assertEqual(self.sb.prioritise([q1, q2], focus), [q2, q1])
        steps = self.sb.revise_steps(focus)
        self.assertEqual(len(steps), 1)
        self.assertIn("Laplace transform", steps[0]["text"])
        self.assertIn(conf.tutor_name(), steps[0]["text"])
        # The brief: a weak-topics section with the tutor note to revise from, and
        # the tutor note cited for a week not written up.
        job = {"module": "Signals and Systems",
               "done": {"deadline": {"due": "2026-11-24", "weight": "30%", "brief_unread": False,
                                     "path": os.path.join(self.ss, "Coursework", "Coursework.md")},
                        "coursework": {"items": [q1, q2], "feeds": [], "has_questions": False},
                        "weeks": [{"week": 5, "topic": "Laplace", "note": "Signals and Systems Week 5",
                                   "written": False, "status": "scaffolded"}],
                        "tasks": [], "plan": [("2026-11-12", steps[0]["text"])],
                        "tutor": json.loads(json.dumps(focus))}}  # as the job file stores it
        text = self.sb._brief_text(job, DAY)
        self.assertIn("## Weak topics first", text)
        self.assertIn("**Laplace transform**: revise [[Signals and Systems Week 5 tutor|Week 5]]", text)
        self.assertIn("needed for Q2 table", text)
        self.assertIn("tutor notes for weeks not written up: [[Signals and Systems Week 5 tutor|Week 5]]", text)
        self.assertLess(text.index("## Weak topics first"), text.index("## What's left"))
        # Without the tutor's record the same job gives the brief it always did.
        plain = dict(job, done={k: v for k, v in job["done"].items() if k != "tutor"})
        self.assertNotIn("Weak topics", self.sb._brief_text(plain, DAY))


if __name__ == "__main__":
    unittest.main()
