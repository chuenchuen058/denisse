"""tutor/note_cards.py: python -m tutor cards --note PATH --deck NAME.

A temp study note outside the fixture vault, a fake writer and checker, a
fake AnkiConnect on a loopback port, a fake battery reading and a fake GPU
lease: never the real vault, Ollama, Anki, the claude CLI or the repo's
.review/gpu.lease.

    .venv\\Scripts\\python.exe -m unittest discover -s tutor -p "test_*.py" -v
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import anki, cards, conf, note_cards, rules  # noqa: E402
from tutor import __main__ as cli  # noqa: E402
from tutor.test_don import (DonFixture, FakeAnki, Server, closed_port_url, fake_cards,  # noqa: E402
                            passing_checks)

CFG = conf.CONFIG
SC = CFG["cards"]["note"]["self_check"]
DECK = "PTB-XL MI"

ANSWERED_Q = r"What does an $\mathrm{AUC}=0.5$ mean for the MI classifier?"
ANSWERED_A = "It ranks MI and normal ECGs no better than chance."
OPEN_Q = "Why must the folds be split by patient rather than by record?"
NOTE = f"""---
name: learn
topic: ECG MI detection
---
# Learning the PTB-XL MI project

The ROC curve plots the true positive rate against the false positive rate as the threshold moves.
The area under it, the AUC, summarises the ranking in one number.

## Self-check

1. {ANSWERED_Q}
   Answer: {ANSWERED_A}
2. {OPEN_Q}
"""


def writer(log):
    """The card writer of test_don for the card kinds; for the self-check
    prompt: the note's two questions (one answered in the note, one not, for
    which it invents an answer anyway) and one the note never asks."""
    cards_ = fake_cards(log)

    def call(system, user):
        if SC["schema"] not in user:
            return cards_(system, user)
        log.append(("selfcheck", user))
        return json.dumps({"questions": [
            {"question": ANSWERED_Q, "answer": ANSWERED_A, "topic": "ROC"},
            {"question": OPEN_Q, "answer": "Because the model wrote this itself.", "topic": "Validation"},
            {"question": "What is the capital of France?", "answer": "Paris", "topic": "Geography"}]})
    return call


class FakeLease:
    def __init__(self, free=True):
        self.free, self.acquired, self.released = free, 0, 0

    def acquire(self, wait_s=0, poll_s=None):
        if self.free:
            self.acquired += 1
        return self.free

    def release(self):
        self.released += 1

    def info(self):
        return None if self.free else {"holder": "eval/run_eval.py", "pid": 4242}


def ac():
    return {"ac": True, "battery_percent": 90}


class NoteCardsTests(DonFixture):
    def setUp(self):
        super().setUp()
        self.fake = FakeAnki()
        self.server = Server(self.fake)
        self.addCleanup(self.server.close)
        self.client = anki.Connect(url=self.server.url, timeout=5)
        self.project = os.path.join(self.tmp, "ptbxl-mi", "docs")  # outside the vault
        os.makedirs(self.project)
        self.learn = os.path.join(self.project, "LEARN.md")
        self.write_note(NOTE)
        self.log = []
        self.lease = FakeLease()
        handoff = unittest.mock.patch.object(rules, "handoff", return_value=True)
        self.handoff = handoff.start()
        self.addCleanup(handoff.stop)

    def write_note(self, text):
        with open(self.learn, "w", encoding="utf-8") as f:
            f.write(text)

    def cli(self, *extra, client=None, power=ac, lease=None, log=None):
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = cli.main(["cards", "--note", self.learn, "--deck", DECK, "--vault", self.vault, "--state", self.state,
                           *extra], calls={"local": writer(self.log if log is None else log),
                                           "claude": writer(self.log if log is None else log)},
                          checks=passing_checks(), anki_client=client or self.client, power=power,
                          lease=lease or self.lease)
        return rc, out.getvalue()

    def deck_notes(self):
        name = anki.deck_name(note_cards.deck(DECK))
        return [n for n in self.fake.notes.values() if n["deckName"] == name]

    def store(self):
        return json.loads(self.read(self.paths.data(note_cards.deck(DECK).card_file)))

    def snapshot(self):
        out = {}
        for root, _dirs, files in os.walk(self.tmp):
            for f in files:
                p = os.path.join(root, f)
                with open(p, "rb") as fh:
                    out[p] = hashlib.sha256(fh.read()).hexdigest()
        return out

    def test_cards_made_self_checks_kept_and_synced_under_the_tutor_deck(self):
        rc, said = self.cli()
        self.assertEqual(rc, 0, said)
        notes = self.deck_notes()
        self.assertEqual(anki.deck_name(note_cards.deck(DECK)), f"{conf.tutor_name()}::{DECK}")
        self.assertTrue(notes)
        fronts = {n["fields"]["Front"]: n["fields"]["Back"] for n in notes}
        kinds = {c["kind"] for c in cards.checked_cards(self.store())}
        self.assertTrue(set(cards.active_kinds()) <= kinds, kinds)  # the module path's kinds
        self.assertIn(SC["kind"], kinds)
        # The answered self-check: the note's answer, and its maths converted once for Anki.
        q = next(f for f in fronts if "MI classifier" in f)
        self.assertIn(r"\(\mathrm{AUC}=0.5\)", q)
        self.assertNotIn("$", q)
        self.assertEqual(q.count(r"\("), 1)
        self.assertEqual(fronts[q], ANSWERED_A)
        # The open one: question kept, the model's invented answer not used.
        open_q = next(f for f in fronts if "split by patient" in f)
        self.assertNotIn("wrote this itself", fronts[open_q])
        self.assertEqual(fronts[open_q], SC["no_answer"].format(source="LEARN"))
        # A question the note never asks is left out.
        self.assertFalse([f for f in fronts if "France" in f])
        # The card store: keyed by the note's path with its content hash.
        store = self.store()
        self.assertEqual(store["deck"], DECK)
        entry = store["sources"][os.path.abspath(self.learn).replace(os.sep, "/")]
        self.assertEqual(entry["hash"], cards._hash(cards.sources.frontmatter(NOTE)[1]))
        # Cards from a note outside the vault link the file, not a vault note.
        self.assertTrue(all(n["fields"]["Source"].startswith('<a href="file:///') for n in notes))
        self.handoff.assert_called()

    def test_rerun_is_idempotent_and_a_change_updates_not_duplicates(self):
        self.cli()
        calls, n = len(self.log), len(self.deck_notes())
        ids = {nn["fields"]["Id"] for nn in self.deck_notes()}
        rc, said = self.cli()
        self.assertEqual(rc, 0, said)
        self.assertEqual(len(self.log), calls)  # unchanged note: no model call
        self.assertEqual(len(self.deck_notes()), n)
        self.assertIn(CFG["say"]["anki_sync"].format(deck=anki.deck_name(note_cards.deck(DECK)), added=0, updated=0,
                                                     same=n, failed=0), said)
        # The note changes: asked again, same ids, nothing added twice.
        self.write_note(NOTE + "\nThe threshold is chosen on the validation folds only.\n")
        rc, said = self.cli()
        self.assertEqual(rc, 0, said)
        self.assertGreater(len(self.log), calls)
        self.assertEqual({nn["fields"]["Id"] for nn in self.deck_notes()}, ids)
        self.assertEqual(len(self.store()["sources"]), 1)

    def test_nothing_written_outside_the_don_area_and_the_note_is_untouched(self):
        before = self.snapshot()
        rc, said = self.cli()
        self.assertEqual(rc, 0, said)
        after = self.snapshot()
        changed = [p for p, h in after.items() if before.get(p) != h]
        don_dir = os.path.normcase(os.path.abspath(self.paths.don_dir)) + os.sep
        self.assertTrue(changed)
        self.assertEqual([p for p in changed if not os.path.normcase(os.path.abspath(p)).startswith(don_dir)], [])
        self.assertEqual(after[self.learn], before[self.learn])
        self.assertIn(self.paths.data(note_cards.deck(DECK).card_file), changed)

    def test_anki_down_is_recorded_unsynced_then_synced_next_run(self):
        down = anki.Connect(url=closed_port_url(), timeout=1)
        rc, said = self.cli(client=down)
        self.assertEqual(rc, 0, said)
        key = note_cards.pending_key(note_cards.deck(DECK))
        self.assertIn(key, rules.pending(self.paths))
        self.assertEqual(self.deck_notes(), [])
        self.assertEqual(self.handoff.call_args.kwargs["synced"], CFG["rules"]["handoff"]["unsynced"])
        rc, said = self.cli()
        self.assertEqual(rc, 0, said)
        self.assertNotIn(key, rules.pending(self.paths))
        self.assertTrue(self.deck_notes())
        # a module run never drops a note deck from the list as gone
        rules.mark_synced(self.paths, [], [key])
        cli._sync_pending(self.paths, down, None, say=lambda s: None)
        self.assertIn(key, rules.pending(self.paths))

    def test_battery_and_gpu_lease(self):
        rc, said = self.cli(power=lambda: {"ac": False, "battery_percent": 50})
        self.assertEqual(rc, 1)
        self.assertIn("battery", said)
        self.assertEqual(self.log, [])
        self.assertFalse(os.path.exists(self.paths.data(note_cards.deck(DECK).card_file)))
        busy = FakeLease(free=False)
        rc, said = self.cli(lease=busy)
        self.assertEqual(rc, 1)
        self.assertIn("eval/run_eval.py", said)
        self.assertEqual(self.log, [])
        rc, said = self.cli(power=lambda: {"ac": None})  # unknown counts as AC
        self.assertEqual(rc, 0, said)
        self.assertEqual((self.lease.acquired, self.lease.released), (1, 1))

    def test_deck_is_required(self):
        with unittest.mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["cards", "--note", self.learn, "--vault", self.vault, "--state", self.state])

    def test_long_note_read_in_parts_each_its_own_source(self):
        src = note_cards.read(self.paths, self.learn)
        pieces = note_cards.parts(src, 200)
        self.assertGreater(len(pieces), 1)
        self.assertEqual(len({p.rel for p in pieces}), len(pieces))
        self.assertEqual(note_cards.parts(src, 10 ** 6)[0].rel, src.rel)


if __name__ == "__main__":
    unittest.main()
