"""tutor/rules.py: Don reads Chuen's rules note and follows it.

A temp vault with a rules note, a fake AnkiConnect (in memory) and fake
models; never the real vault, Anki, Ollama or the network.

    .venv\\Scripts\\python.exe -m unittest tutor.test_rules -v
"""
from __future__ import annotations

import contextlib
import copy
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

from tutor import anki, cards, conf, don, exam, rules, teach  # noqa: E402
from tutor import __main__ as cli  # noqa: E402
from tutor.test_don import DonFixture, FakeAnki, closed_port_url, fake_cards, passing_checks  # noqa: E402

CFG = conf.CONFIG
_KINDS = None

# Rule wording the code must never contain: it only reaches a prompt from the note.
CARD_RULE = "Wrong is worse than missing, and a card in purple ink is never kept."
REPORT_RULE = "Mark each part straight after he answers it, never at the end."
RUNNING_RULE = "Only on mains power."
MATERIAL_RULE = "Read new KEATS material the same day."


def rules_note(date: str) -> str:
    return ("---\nname: don-rules\ndescription: rules\nupdated: 2026-11-09\ntype: rules\n---\n\n"
            "# Don's rules\n\nEdit any line.\n\n"
            "## Cards\n\n1. **Stay on the module.** Only the module's material.\n"
            f"4. {CARD_RULE}\n\n"
            f"## Running\n\n6. {RUNNING_RULE}\n\n"
            f"## Reporting\n\n8. {MATERIAL_RULE}\n12. {REPORT_RULE}\n\n"
            "## Exams\n\n| Exam | Module | Weeks | Date |\n| --- | --- | --- | --- |\n"
            f"| Mock | Signals and Systems | 1–5 | {date} |\n")


def setUpModule():
    """The core kinds only, as test_don pins them (the style kind has its own tests)."""
    global _KINDS
    _KINDS = copy.deepcopy(CFG["cards"]["kinds"])
    for k in [k for k, v in _KINDS.items() if v.get("style")]:
        del CFG["cards"]["kinds"][k]


def tearDownModule():
    CFG["cards"]["kinds"].clear()
    CFG["cards"]["kinds"].update(_KINDS)


class ScopedAnki(FakeAnki):
    """FakeAnki plus what the exam scope needs: tags, suspension, and the
    note -> cards link notesInfo gives."""

    def __init__(self):
        super().__init__()
        self.suspended = set()

    def handle(self, req):
        action, p = req["action"], req.get("params", {})
        if action == "notesInfo":
            out = super().handle(req)
            for info in out:
                n = self.notes[info["noteId"]]
                info["tags"] = list(n.get("tags", []))
                info["cards"] = [n["card"]]
            return out
        if action in ("addTags", "removeTags"):
            self.calls.append(action)
            for nid in p["notes"]:
                tags = self.notes[nid].setdefault("tags", [])
                for t in p["tags"].split():
                    if action == "addTags" and t not in tags:
                        tags.append(t)
                    elif action == "removeTags" and t in tags:
                        tags.remove(t)
            return None
        if action in ("suspend", "unsuspend"):
            self.calls.append(action)
            (self.suspended.update if action == "suspend" else self.suspended.difference_update)(p["cards"])
            return True
        return super().handle(req)


class Client:
    """An in-process AnkiConnect client over a fake (no socket at all)."""
    url = "in-process"

    def __init__(self, fake, up=True):
        self.fake, self.up = fake, up

    def __call__(self, action, **params):
        if not self.up:
            raise anki.AnkiError("down")
        try:
            return self.fake.handle({"action": CFG["anki"]["connect"]["actions"][action], "params": params})
        except Exception as exc:  # noqa: BLE001 -- AnkiConnect reports errors as errors
            raise anki.AnkiError(str(exc)) from exc

    def reachable(self):
        return (True, "") if self.up else (False, "down")


class RulesFixture(DonFixture):
    def setUp(self):
        super().setUp()
        rules.reset()
        self.addCleanup(rules.reset)
        self.note_path = rules.note_path(self.paths)
        self.write_rules((self.today + dt.timedelta(days=3)).isoformat())

    def write_rules(self, date):
        os.makedirs(os.path.dirname(self.note_path), exist_ok=True)
        with open(self.note_path, "w", encoding="utf-8") as f:
            f.write(rules_note(date))
        # A fresh mtime even inside one clock tick: rules.load caches on mtime,
        # so each rewrite must land strictly after the one before it.
        st = os.stat(self.note_path)
        mtime = max(st.st_mtime, getattr(self, "_note_mtime", 0)) + 1
        self._note_mtime = mtime
        os.utime(self.note_path, (st.st_atime, mtime))

    def card(self, n, week, topic="Laplace transform"):
        return {"id": f"card-{n}", "kind": "recall", "front": f"Question {n}?", "back": f"Answer {n}",
                "topic": topic, "source": "Atlas/x.md", "source_name": "x", "week": week,
                "check": {"verdict": CFG["cards"]["check"]["verdicts"]["right"]}}


class NoteTests(RulesFixture):
    def test_note_is_read_into_sections_and_exams(self):
        r = rules.load(self.paths)
        self.assertTrue(r.found)
        self.assertEqual(list(r.sections)[1:], ["Cards", "Running", "Reporting", "Exams"])
        [mock] = rules.exams(self.paths)
        self.assertEqual(mock.name, "Mock")
        self.assertEqual(mock.module.slug, self.ss_module.slug)
        self.assertEqual(mock.weeks, [1, 2, 3, 4, 5])
        self.assertEqual(mock.date, self.today + dt.timedelta(days=3))
        self.assertEqual(mock.tag, "after-mock")
        self.assertEqual(rules.scope_weeks(self.paths, self.ss_module), {1, 2, 3, 4, 5})

    def test_prompt_blocks_carry_the_notes_own_sections(self):
        rules.load(self.paths)
        cards_text = rules.block("cards")
        self.assertIn(CARD_RULE, cards_text)
        self.assertNotIn(RUNNING_RULE, cards_text)
        self.assertNotIn("| Mock |", cards_text)  # the table is machinery, not a prompt
        self.assertIn(MATERIAL_RULE, rules.block("explain"))
        self.assertIn(REPORT_RULE, rules.block("quiz"))
        self.assertIn(REPORT_RULE, rules.block("explain"))

    def test_a_renamed_heading_gives_the_whole_note(self):
        with open(self.note_path, encoding="utf-8") as f:
            text = f.read().replace("## Cards", "## Flashcards")
        with open(self.note_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.utime(self.note_path, (1, 2))
        rules.load(self.paths)
        self.assertIn(CARD_RULE, rules.block("cards"))
        self.assertIn(RUNNING_RULE, rules.block("cards"))

    def test_missing_note_runs_with_defaults_and_says_one_line(self):
        os.remove(self.note_path)
        said = []
        for _ in range(3):
            r = rules.load(self.paths, say=said.append)
        self.assertFalse(r.found)
        self.assertEqual(len(said), 1)
        self.assertIn(self.note_path, said[0])
        self.assertEqual(rules.block("cards"), "")
        self.assertEqual(rules.exams(self.paths), [])
        self.assertIsNone(rules.scope_weeks(self.paths, self.ss_module))

    def test_unreadable_note_is_the_defaults_too(self):
        with open(self.note_path, "wb") as f:
            f.write(b"\xff\xfe\xfa not utf-8 \xc3\x28")
        said = []
        r = rules.load(self.paths, say=said.append)
        self.assertFalse(r.found)
        self.assertEqual(len(said), 1)

    def test_weeks_and_dates(self):
        self.assertEqual(rules.weeks_of("1–5"), [1, 2, 3, 4, 5])
        self.assertEqual(rules.weeks_of("2, 4-5"), [2, 4, 5])
        self.assertEqual(rules.date_of("2026-11-20"), dt.date(2026, 11, 20))
        self.assertEqual(rules.date_of("Friday 20 November 2026, 10:00"), dt.date(2026, 11, 20))
        self.assertIsNone(rules.date_of("in the next few days (exact date to add)"))

    def test_undated_exam_is_active(self):
        self.write_rules("in the next few days (exact date to add)")
        rules.load(self.paths)
        self.assertEqual(rules.scope_weeks(self.paths, self.ss_module), {1, 2, 3, 4, 5})

    def test_past_exam_is_not_active_and_the_switch_turns_it_off(self):
        rules.load(self.paths)
        with unittest.mock.patch.dict(CFG["rules"]["exam_scope"], {"enabled": False}):
            self.assertIsNone(rules.scope_weeks(self.paths, self.ss_module))
        self.write_rules((self.today - dt.timedelta(days=1)).isoformat())
        rules.load(self.paths)
        self.assertIsNone(rules.scope_weeks(self.paths, self.ss_module))


class PromptTests(RulesFixture):
    def test_card_writer_and_checker_see_the_rules(self):
        rules.load(self.paths)
        systems = []

        def capture(fn):
            def call(system, user):
                systems.append(system)
                return fn(system, user)
            return call
        checks = {k: capture(v) for k, v in passing_checks().items()}
        log = []
        self.make_cards(log, calls={"local": capture(fake_cards(log))}, checks=checks)
        self.assertTrue(systems)
        self.assertTrue(all(CARD_RULE in s for s in systems), "every card and check prompt carries the note")

    def test_no_note_leaves_the_prompts_as_they_were(self):
        os.remove(self.note_path)
        rules.load(self.paths, say=lambda _s: None)
        systems = []

        def call(system, user):
            systems.append(system)
            return fake_cards([])(system, user)
        self.make_cards(calls={"local": call})
        self.assertTrue(systems)
        self.assertFalse(any(CFG["rules"]["prompt_block"].split("{")[0].strip() in s for s in systems))

    def test_quiz_marking_and_lessons_see_the_rules(self):
        rules.load(self.paths)
        seen = []

        def mark(system, user):
            seen.append(system)
            return json.dumps({"results": [{"n": 1, "correct": True, "comment": "ok"}]})
        exam.mark_quiz(self.ss_module, {"cards": [{"n": 1, "front": "Q?", "back": "A"}]}, "A",
                       calls={CFG["quiz"]["mark_engine"]: mark})
        self.assertIn(REPORT_RULE, seen[-1])
        system, _user = teach.prompt(self.ss_module, "Laplace", {"weeks": [], "text": "x"})
        self.assertIn(REPORT_RULE, system)


class ExamScopeTests(RulesFixture):
    def setUp(self):
        super().setUp()
        rules.load(self.paths)
        self.fake = ScopedAnki()
        self.client = Client(self.fake)
        self.deck = [self.card(1, 2), self.card(2, 7), self.card(3, None), self.card(4, 9)]
        anki.push(self.paths, self.client, self.ss_module, self.deck)
        self.by_id = {n["fields"][CFG["anki"]["field_of"]["id"]]: nid for nid, n in self.fake.notes.items()}

    def scoped(self, today=None):
        return anki.apply_scope(self.paths, self.client, self.ss_module, self.deck, today or self.today,
                                say=self.out)

    def in_anki(self, n):
        nid = self.by_id[f"card-{n}"]
        note = self.fake.notes[nid]
        return note["card"] in self.fake.suspended, [t for t in note.get("tags", []) if t.startswith("after-")]

    def test_cards_outside_the_exam_weeks_are_suspended_and_tagged(self):
        out = self.scoped()
        self.assertEqual(out, {"suspended": 2, "unsuspended": 0})
        self.assertEqual(self.in_anki(1), (False, []))           # week 2: in scope
        self.assertEqual(self.in_anki(2), (True, ["after-mock"]))  # week 7: after the mock
        self.assertEqual(self.in_anki(3), (False, []))           # no week: never out of scope
        self.assertEqual(self.in_anki(4), (True, ["after-mock"]))
        self.assertEqual(self.scoped(), {"suspended": 0, "unsuspended": 0})  # idempotent
        self.assertIn("exam scope", self.say.getvalue())

    def test_after_the_date_they_come_back_and_his_own_suspensions_stay(self):
        self.scoped()
        his = self.fake.notes[self.by_id["card-1"]]["card"]
        self.fake.suspended.add(his)  # he suspended an in-scope card himself
        out = self.scoped(self.today + dt.timedelta(days=4))
        self.assertEqual(out, {"suspended": 0, "unsuspended": 2})
        self.assertEqual(self.in_anki(2), (False, []))
        self.assertEqual(self.in_anki(4), (False, []))
        self.assertIn(his, self.fake.suspended)

    def test_a_row_taken_out_of_the_table_unsuspends_too(self):
        self.scoped()
        with open(self.note_path, encoding="utf-8") as f:
            text = f.read().split("## Exams")[0]
        with open(self.note_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.utime(self.note_path, (5, 6))
        rules.load(self.paths)
        self.assertEqual(self.scoped(), {"suspended": 0, "unsuspended": 2})

    def test_quiz_stays_inside_the_scope(self):
        deck = cards.load(self.paths, self.ss_module)
        deck["sources"] = {"Atlas/x.md": {"cards": self.deck}}
        don.save_json(self.paths, cards.sidecar(self.ss_module), deck)
        picked = exam.pick_quiz(self.paths, self.ss_module, n=10)
        self.assertEqual(sorted(c["id"] for c in picked), ["card-1", "card-3"])
        with unittest.mock.patch.dict(CFG["rules"]["exam_scope"], {"enabled": False}):
            self.assertEqual(len(exam.pick_quiz(self.paths, self.ss_module, n=10)), 4)


class WeakTopicTests(RulesFixture):
    def test_the_weakest_topic_in_scope_is_chosen(self):
        rules.load(self.paths)
        don.save_json(self.paths, CFG["don"]["anki_file"].format(slug=self.ss_module.slug), {"topics": [
            {"topic": "Bode plots", "weak": True, "weeks": [8]},
            {"topic": "Laplace transform", "weak": True, "weeks": [5]},
            {"topic": "Fourier transform", "weak": False, "weeks": [4]}]})
        self.assertEqual(rules.weakest(self.paths, self.ss_module), "Laplace transform")  # week 8 is after the mock
        self.write_rules((self.today - dt.timedelta(days=1)).isoformat())
        rules.load(self.paths)
        self.assertEqual(rules.weakest(self.paths, self.ss_module), "Bode plots")
        with unittest.mock.patch.dict(CFG["rules"]["weak_topics"], {"enabled": False}):
            self.assertEqual(rules.weakest(self.paths, self.ss_module), "")

    def test_an_exam_question_with_no_topic_takes_the_weakest(self):
        rules.load(self.paths)
        don.save_json(self.paths, CFG["don"]["anki_file"].format(slug=self.ss_module.slug), {"topics": [
            {"topic": "Laplace transform", "weak": True, "weeks": []}]})
        users = []

        def setter(system, user):
            users.append(user)
            return json.dumps({"title": "T", "question": "Find the Laplace transform of a step.", "marks": 5,
                               "mark_scheme": [{"step": "s", "marks": 5}]})
        item = exam.set_exam(self.paths, self.ss_module, "", calls={CFG["exam"]["engine"]: setter})
        self.assertEqual(item["topic"], "Laplace transform")


class CliTests(RulesFixture):
    def run_cli(self, *argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main([*argv, "--vault", self.vault, "--state", self.state], **kw)
        return rc, out.getvalue(), err.getvalue()

    def handoff_lines(self):
        from atrium import handoff
        p = handoff.path(vault=self.vault)
        if not p.exists():
            return []
        tag = "[" + CFG["rules"]["handoff"]["agent"].format(**don.ctx()) + "]"
        return [l for l in p.read_text(encoding="utf-8").splitlines() if tag in l]

    def test_cards_syncs_to_anki_at_the_end_and_writes_one_handoff_line(self):
        fake = ScopedAnki()
        rc, out, _err = self.run_cli("cards", "signals", calls={"local": fake_cards([])},
                                     checks=passing_checks(), anki_client=Client(fake))
        self.assertEqual(rc, 0)
        self.assertTrue(fake.notes, "the checked cards reached Anki")
        self.assertEqual(rules.pending(self.paths), [])
        [line] = self.handoff_lines()
        self.assertIn("cards Signals and Systems", line)
        self.assertIn(CFG["rules"]["handoff"]["synced"], line)

    def test_anki_down_records_unsynced_and_the_next_run_syncs_first(self):
        rc, _out, _err = self.run_cli("cards", "signals", calls={"local": fake_cards([])},
                                      checks=passing_checks(), anki_client=Client(None, up=False))
        self.assertEqual(rc, 0)
        self.assertEqual(rules.pending(self.paths), [self.ss_module.slug])
        self.assertIn(CFG["rules"]["handoff"]["unsynced"], self.handoff_lines()[-1])
        fake = ScopedAnki()
        rc, out, _err = self.run_cli("progress", anki_client=Client(fake))
        self.assertEqual(rc, 0)
        self.assertTrue(fake.notes, "the next job synced first")
        self.assertEqual(rules.pending(self.paths), [])
        self.assertIn(CFG["rules"]["sync_after_cards"]["say_pending"].format(n=1).strip(), out)

    def test_the_switch_turns_the_sync_off(self):
        with unittest.mock.patch.dict(CFG["rules"]["sync_after_cards"], {"enabled": False}):
            fake = ScopedAnki()
            self.run_cli("cards", "signals", calls={"local": fake_cards([])}, checks=passing_checks(),
                         anki_client=Client(fake))
        self.assertFalse(fake.notes)

    def test_a_second_batch_job_exits_with_one_line_while_one_runs(self):
        with unittest.mock.patch.dict(CFG["rules"]["lock"], {"wait_s": 0}):
            with rules.batch_lock(self.paths, "ingest"):
                rc, out, _err = self.run_cli("cards", "signals", calls={"local": fake_cards([])},
                                             checks=passing_checks(), anki_client=Client(ScopedAnki()))
            self.assertEqual(rc, 1)
            self.assertEqual(len(out.strip().splitlines()), 1)
            self.assertIn("ingest", out)
            self.assertIn("not started", self.handoff_lines()[-1])
            # Free again: the job runs.
            rc, _out, _err = self.run_cli("cards", "signals", calls={"local": fake_cards([])},
                                          checks=passing_checks(), anki_client=Client(ScopedAnki()))
            self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(rules.lock_path(self.paths)))

    def test_a_stale_lock_is_taken_over_and_chat_commands_take_none(self):
        with open(rules.lock_path(self.paths), "w", encoding="utf-8") as f:
            json.dump({"pid": 999999999, "command": "ingest"}, f)
        self.addCleanup(lambda: os.path.exists(rules.lock_path(self.paths)) and os.remove(rules.lock_path(self.paths)))
        with rules.batch_lock(self.paths, "cards", wait_s=0) as held:
            self.assertTrue(held)
            with rules.batch_lock(self.paths, "quiz", wait_s=0) as none:
                self.assertIsNone(none)

    def test_the_lock_waits_then_gives_up(self):
        naps = []
        with rules.batch_lock(self.paths, "ingest"):
            with self.assertRaises(rules.Busy) as busy:
                with rules.batch_lock(self.paths, "cards", say=self.out, wait_s=30, sleep=naps.append):
                    pass
        self.assertEqual(sum(naps), 30)
        self.assertIn("cards not started", busy.exception.line)
        self.assertIn("waiting up to 30s", self.say.getvalue())

    def test_missing_rules_note_is_one_line_on_stderr(self):
        os.remove(self.note_path)
        rules.reset()
        rc, out, err = self.run_cli("topics", "signals")
        self.assertEqual(err.count(CFG["rules"]["missing"].split("{path}")[0].format(**don.ctx())), 1)
        self.assertNotIn(self.note_path, out)

    def test_exam_style_kind_off_is_said(self):
        said = []
        self.assertEqual(rules.check_kinds(say=said.append), [k for k in CFG["rules"]["exam_style_kinds"]["kinds"]
                                                              if k not in CFG["cards"]["kinds"]])
        with unittest.mock.patch.dict(CFG["cards"]["kinds"]["problem"], {"count": 0}):
            said = []
            self.assertIn("problem", rules.check_kinds(say=said.append))
            self.assertTrue(any("problem" in s for s in said))

    def test_the_shipped_card_mix_has_the_exam_style_kinds_every_week(self):
        for k in CFG["rules"]["exam_style_kinds"]["kinds"]:
            self.assertGreater(_KINDS[k]["count"], 0, k)


class NotOursTests(RulesFixture):
    def test_a_file_that_isnt_dons_is_said_once(self):
        path = os.path.join(self.paths.don_dir, "x.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for p in (path, os.path.splitext(path)[0] + CFG["note"]["copy_suffix"] + ".md"):
            with open(p, "w", encoding="utf-8") as f:
                f.write("his\n")
        state = {"written": {}}
        said = []
        for _ in range(3):
            self.assertIsNone(don.write_note(path, "mine\n", state, say=said.append))
        self.assertEqual(len(said), 1)
        self.assertIn("isn't", said[0])
        with unittest.mock.patch.dict(CFG["rules"]["not_ours_once"], {"enabled": False}):
            don.write_note(path, "mine\n", state, say=said.append)
        self.assertEqual(len(said), 2)


class NoRuleTextInCodeTests(unittest.TestCase):
    def test_the_notes_wording_is_not_in_code(self):
        """The rules reach prompts from the note; no rule sentence is copied into code."""
        with open(os.path.join(REPO, "tutor", "rules.py"), encoding="utf-8") as f:
            code = f.read()
        for phrase in ("Stay on the module", "Wrong is worse", "Mains power", "part by part", "Be blunt"):
            self.assertNotIn(phrase, code)


if __name__ == "__main__":
    unittest.main()
