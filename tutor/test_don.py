"""tutor/ part 2: Don in the vault, cards, Anki -- fixture vault, fake model,
fake AnkiConnect.

Never touches the real vault, memory/, Ollama, Anki or the network: the
fixture vault and inbox come from test_tutor.Fixture, the models are
functions, AnkiConnect is a local in-process HTTP server on a free loopback
port, and the learner model's store and note are pointed into the fixture.

    .venv\\Scripts\\python.exe -m unittest discover -s tutor -p "test_*.py" -v
"""
from __future__ import annotations

import copy
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import anki, cards, conf, don, sources  # noqa: E402
from tutor import __main__ as cli  # noqa: E402
from tutor.test_tutor import HERE, MODULES, SS, Fixture  # noqa: E402

CFG = conf.CONFIG


# -- fakes -------------------------------------------------------------------------

def fake_cards(log, tag="local"):
    """A card writer that answers for exactly the kinds it is asked for,
    with a topic named after what the note mentions."""
    def call(system, user):
        log.append((tag, user))
        kinds = re.findall(r'\(kind: "(\w+)"\)', user)
        text = user.split("NOTE:\n", 1)[1]
        source = user.split("Source note: ", 1)[1].split(".\n", 1)[0]
        topic = "Laplace transform" if "Laplace" in text else "Fourier transform" if "Fourier" in text \
            else "Coursework"
        out = []
        for kind in kinds:
            for n in range(1, CFG["cards"]["kinds"][kind]["count"] + 2):  # one too many: capped
                out.append({"kind": kind, "front": f"{tag} {kind} {n} from {source}: {topic}?", "back": f"Answer {n}\nline two",
                            "topic": topic})
        out.append({"kind": "essay", "front": "not a kind", "back": "x"})
        return json.dumps({"cards": out})
    return call


class FakeAnki:
    """Enough of AnkiConnect for tutor/anki.py, in memory."""

    def __init__(self):
        self.decks, self.models, self.notes, self.cards = set(), {}, {}, {}
        self.calls = []
        self.next_id = 1000

    def handle(self, req):
        action, p = req["action"], req.get("params", {})
        self.calls.append(action)
        if action == "version":
            return 6
        if action == "createDeck":
            self.decks.add(p["deck"])
            return 1
        if action == "modelNames":
            return sorted(self.models)
        if action == "createModel":
            self.models[p["modelName"]] = p
            return {}
        if action in ("findNotes", "findCards"):
            hits = [nid for nid, n in self.notes.items()
                    if f'"deck:{n["deckName"]}"' in p["query"] and f'"note:{n["modelName"]}"' in p["query"]]
            return hits if action == "findNotes" else [self.notes[n]["card"] for n in hits]
        if action == "notesInfo":
            return [{"noteId": nid, "modelName": self.notes[nid]["modelName"],
                     "fields": {k: {"value": v, "order": i} for i, (k, v) in enumerate(self.notes[nid]["fields"].items())}}
                    for nid in p["notes"]]
        if action == "addNote":
            n = p["note"]
            assert n["modelName"] in self.models and n["deckName"] in self.decks
            first = list(n["fields"].values())[0]
            if any(x["fields"] and list(x["fields"].values())[0] == first and x["deckName"] == n["deckName"]
                   for x in self.notes.values()):
                raise ValueError("cannot create note because it is a duplicate")
            self.next_id += 2
            nid, cid = self.next_id, self.next_id + 1
            self.notes[nid] = {**copy.deepcopy(n), "card": cid}
            self.cards[cid] = {"note": nid, "reps": 0, "lapses": 0, "factor": 0, "due": False}
            return nid
        if action == "updateNoteFields":
            self.notes[p["note"]["id"]]["fields"].update(p["note"]["fields"])
            return None
        if action == "cardsInfo":
            out = []
            for cid in p["cards"]:
                c = self.cards[cid]
                n = self.notes[c["note"]]
                out.append({"cardId": cid, "note": c["note"], "deckName": n["deckName"],
                            "fields": {k: {"value": v, "order": i} for i, (k, v) in enumerate(n["fields"].items())},
                            "reps": c["reps"], "lapses": c["lapses"], "factor": c["factor"], "interval": 1,
                            "type": 2, "queue": 2, "due": 0})
            return out
        if action == "areDue":
            return [self.cards[c]["due"] for c in p["cards"]]
        raise ValueError(f"unsupported action {action}")

    def review(self, topic, lapses, factor, reps=3, due=True):
        """Pretend he reviewed every card on `topic`."""
        for cid, c in self.cards.items():
            if self.notes[c["note"]]["fields"]["Topic"] == topic:
                c.update(reps=reps, lapses=lapses, factor=factor, due=due)


class Server:
    def __init__(self, fake):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                try:
                    body = {"result": fake.handle(req), "error": None}
                except Exception as exc:  # noqa: BLE001 -- AnkiConnect reports errors in the body
                    body = {"result": None, "error": str(exc)}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def closed_port_url():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# -- the fixture ------------------------------------------------------------------

class DonFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.ss_module = next(m for m in conf.modules(self.paths) if m.code == "5CCYB020")
        # Notes of his outside the module folder: one tagged with the code, one linking the hub,
        # one about nothing, one in .obsidian (never read).
        self.note(os.path.join("Atlas", "Concepts", "Laplace tricks.md"),
                  "---\nname: laplace-tricks\ndescription: mine\nupdated: 2026-11-01\ntags: [maths, 5CCYB020]\n---\n"
                  "# Laplace tricks\n\nThe Laplace transform turns a derivative into multiplication by s, minus the "
                  "initial value. Partial fractions first, then read the inverse off the table.\n")
        self.note(os.path.join("Calendar", "2026-11-02 study.md"),
                  "---\nname: study-2026-11-02\ndescription: a study day\nupdated: 2026-11-02\ntags: [log]\n---\n"
                  "Went over [[Signals and Systems notes]] and the Fourier transform of a top hat (a sinc). "
                  "The width and the zero crossings are inversely related; worth remembering for the exam.\n")
        self.note(os.path.join("Atlas", "Concepts", "Unrelated.md"),
                  "---\nname: unrelated\ndescription: other\nupdated: 2026-11-01\ntags: [misc]\n---\n"
                  + "Nothing about any module, just a long enough note to be read if it were wanted. " * 3)
        self.note(os.path.join(".obsidian", "workspace.md"), "Fourier " * 50)
        self.note(os.path.join(MODULES, SS, "My own Laplace summary.md"),
                  "---\nname: my-laplace\ndescription: mine\nupdated: 2026-11-01\ntags: [module]\n---\n"
                  "# Laplace\n\nMy summary: the Laplace transform of a step is 1/s, of an exponential 1/(s+a). "
                  "Region of convergence matters for uniqueness.\n")
        self.say = io.StringIO()
        self.learner_dir = os.path.join(self.tmp, "learner")
        patcher = unittest.mock.patch.multiple(
            "learner.core", STORE=pathlib.Path(self.vault, "Dev", "learner", "model.json"),
            NOTE=pathlib.Path(self.vault, "Atlas", "Concepts", "Learning", "Learner-Model.md"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def out(self, s):
        self.say.write(s + "\n")

    def make_cards(self, log=None, calls=None):
        log = [] if log is None else log
        calls = calls or {"local": fake_cards(log), "claude": fake_cards(log, "claude")}
        return cards.run(self.paths, self.ss_module, calls=calls, today=self.today, say=self.out), log


# -- tests -------------------------------------------------------------------------

class SourceTests(DonFixture):
    def test_whole_vault_not_just_the_inbox(self):
        self.run_ingest()
        found = {s.rel: s for s in sources.collect(self.paths, self.ss_module)}
        rels = set(found)
        base = "Atlas/Academic/SEM 1 Modules/5CCYB020 Signals and Systems/"
        self.assertIn(base + "Week 4/Signals and Systems Week 4 tutor.md", rels)   # ingest's tutor note
        self.assertIn(base + "My own Laplace summary.md", rels)                    # his note in the module folder
        self.assertIn(base + "Coursework/Coursework.md", rels)                     # coursework
        self.assertIn("Atlas/Concepts/Laplace tricks.md", rels)                    # tagged with the code
        self.assertIn("Calendar/2026-11-02 study.md", rels)                        # links the module hub
        self.assertNotIn("Atlas/Concepts/Unrelated.md", rels)
        self.assertFalse([r for r in rels if r.startswith(".obsidian") or "/Don/" in r])
        self.assertNotIn(base + "Signals and Systems tutor.md", rels)              # the index: links only
        self.assertEqual(found[base + "Week 4/Signals and Systems Week 4 tutor.md"].kind, CFG["sources"]["tutor"])
        self.assertEqual(found[base + "Week 4/Signals and Systems Week 4 tutor.md"].week, 4)
        self.assertEqual(found["Atlas/Concepts/Laplace tricks.md"].kind, CFG["sources"]["own"])
        self.assertEqual(found["Atlas/Concepts/Laplace tricks.md"].link, "[[Atlas/Concepts/Laplace tricks|Laplace tricks]]")
        # Nothing of Electromagnetism's.
        em = next(m for m in conf.modules(self.paths) if m.code == "5CCYB010")
        self.assertEqual(sources.collect(self.paths, em), [])


class CardTests(DonFixture):
    def test_cards_kinds_ids_sources_and_dedupe(self):
        self.run_ingest()
        out, log = self.make_cards()
        self.assertEqual(out["failed"], [])
        kinds = CFG["cards"]["kinds"]
        per_note = sum(k["count"] for k in kinds.values())
        self.assertEqual(len(out["cards"]), per_note * len(out["made"]))
        self.assertEqual(len(log), len(out["made"]))  # every kind local by default: one call per note
        card = next(c for c in out["cards"] if c["source"] == "Atlas/Concepts/Laplace tricks.md")
        self.assertEqual(card["topic"], "Laplace transform")
        self.assertTrue(card["id"].startswith(conf.tutor_slug() + "-"))
        self.assertEqual(len({c["id"] for c in out["cards"]}), len(out["cards"]))
        w5 = [c for c in out["cards"] if c["week"] == 5]
        self.assertTrue(w5)
        # The sidecar and the readable note are in the vault's Don area.
        self.assertTrue(os.path.exists(self.paths.data(CFG["don"]["card_file"].format(slug="signals-and-systems"))))
        note = self.read(don.cards_path(self.paths, self.ss_module))
        self.assertIn("## [[Atlas/Concepts/Laplace tricks|Laplace tricks]]", note)
        self.assertIn(f"Anki deck: `{conf.tutor_name()}::Signals and Systems`", note)
        self.assertIn(f"Written by {conf.tutor_name()}", note)
        progress = self.read(don.progress_path(self.paths, self.ss_module))
        self.assertIn(f"{len(out['cards'])} card(s)", progress)
        hub = self.read(don.hub_path(self.paths))
        self.assertIn("**Signals and Systems**", hub)
        # Second run: nothing asked, same ids.
        out2, log2 = self.make_cards()
        self.assertEqual(log2, [])
        self.assertEqual([c["id"] for c in out2["cards"]], [c["id"] for c in out["cards"]])
        # One note changed: only it is asked again, and its ids stay put.
        path = os.path.join(self.vault, "Atlas", "Concepts", "Laplace tricks.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\nAlso: final value theorem.\n")
        out3, log3 = self.make_cards()
        self.assertEqual(out3["made"], ["Atlas/Concepts/Laplace tricks.md"])
        self.assertEqual(len(log3), 1)
        self.assertEqual({c["id"] for c in out3["cards"]}, {c["id"] for c in out["cards"]})
        code, report = self.contract()
        self.assertEqual(code, 0, report)

    def test_hard_problems_go_to_claude_when_switched(self):
        log = []
        kinds = copy.deepcopy(CFG["cards"]["kinds"])
        kinds["problem"]["engine"] = "claude"
        with unittest.mock.patch.dict(CFG["cards"], {"kinds": kinds}):
            out, _ = self.make_cards(log)
        tags = {}
        for tag, user in log:
            tags.setdefault(tag, set()).update(re.findall(r'\(kind: "(\w+)"\)', user))
        self.assertEqual(tags["claude"], {"problem"})
        self.assertEqual(tags["local"], {"recall", "method"})
        self.assertTrue(all(c["engine"] == "claude" for c in out["cards"] if c["kind"] == "problem"))

    def test_delegate_engine_goes_through_delegate_run(self):
        seen = []

        def fake_run(task, context=None, engines=None):
            seen.append(engines)
            return True, json.dumps({"cards": []}), "claude", []
        fake_delegate = unittest.mock.MagicMock(run=fake_run)
        with unittest.mock.patch.dict("sys.modules", {"delegate": fake_delegate}):
            call = cards.engine_call("claude")
            self.assertEqual(call("s", "u"), json.dumps({"cards": []}))
        self.assertEqual(seen, [CFG["cards"]["engines"]["claude"]["delegate_engines"]])

    def test_same_question_from_two_notes_is_one_card(self):
        deck = {"sources": {"a.md": {"cards": [{"front": "What is X?", "id": "1"}]},
                            "b.md": {"cards": [{"front": "what is  x?", "id": "2"}, {"front": "Y?", "id": "3"}]}}}
        self.assertEqual([c["id"] for c in cards.all_cards(deck)], ["1", "3"])

    def test_model_failure_keeps_old_cards(self):
        out, _ = self.make_cards()
        before = len(out["cards"])
        path = os.path.join(self.vault, "Atlas", "Concepts", "Laplace tricks.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\nmore\n")
        out2 = cards.run(self.paths, self.ss_module, calls={"local": lambda s, u: "no"}, today=self.today,
                         say=self.out)
        self.assertEqual(len(out2["failed"]), 1)
        self.assertEqual(len(out2["cards"]), before)

    def test_name_comes_from_config(self):
        with unittest.mock.patch.dict(CFG, {"name": "Tess"}):
            self.assertEqual(anki.deck_name(self.ss_module), "Tess::Signals and Systems")
            self.assertEqual(os.path.basename(don.hub_path(self.paths)), "Tess.md")
            self.assertTrue(cards.card_id(self.ss_module, "x.md", "recall", 1).startswith("tess-"))


class AnkiTests(DonFixture):
    def setUp(self):
        super().setUp()
        self.run_ingest()
        self.made, _ = self.make_cards()
        self.fake = FakeAnki()
        self.server = Server(self.fake)
        self.addCleanup(self.server.close)
        self.client = anki.Connect(url=self.server.url, timeout=5)

    def sync(self, client=None):
        return anki.run(self.paths, [self.ss_module], client=client or self.client, today=self.today, say=self.out)

    def test_unreachable_writes_tsv_and_records_nothing(self):
        out = self.sync(anki.Connect(url=closed_port_url(), timeout=2))
        self.assertFalse(out["reachable"])
        tsv = self.read(out["tsv"][0])
        lines = tsv.splitlines()
        self.assertIn("#guid column:1", lines)
        self.assertIn(f"#deck:{conf.tutor_name()}::Signals and Systems", lines)
        rows = [line for line in lines if not line.startswith("#")]
        self.assertEqual(len(rows), len(self.made["cards"]))
        self.assertTrue(all(len(r.split("\t")) == len(CFG["anki"]["tsv"]["columns"]) for r in rows))
        self.assertIn("obsidian://open?vault=vault&amp;file=", tsv)
        self.assertIn("Answer 1<br>line two", tsv)
        self.assertFalse(os.path.exists(self.paths.data(CFG["don"]["anki_file"].format(slug="signals-and-systems"))))
        self.assertFalse(os.path.exists(os.path.join(self.vault, "Dev", "learner", "model.json")))
        self.assertIn("not reachable", self.say.getvalue())

    def test_add_then_update_never_duplicate(self):
        out = self.sync()
        n = len(self.made["cards"])
        self.assertTrue(out["reachable"])
        self.assertEqual(out["synced"]["signals-and-systems"]["added"], n)
        deck = f"{conf.tutor_name()}::Signals and Systems"
        self.assertIn(deck, self.fake.decks)
        model = self.fake.models[anki.note_type()]
        self.assertEqual(model["inOrderFields"], ["Front", "Back", "Source", "Topic", "Id"])
        self.assertEqual(len(self.fake.notes), n)
        note = next(iter(self.fake.notes.values()))
        self.assertIn(conf.tutor_slug(), note["tags"])
        self.assertTrue(any(t.startswith("topic::") for t in note["tags"]))
        # Again: nothing added, nothing changed.
        out2 = self.sync()
        self.assertEqual(out2["synced"]["signals-and-systems"], {"added": 0, "updated": 0, "same": n,
                                                                 "failed": 0, "errors": []})
        self.assertEqual(len(self.fake.notes), n)
        # A card regenerated with a new answer: updated in place.
        path = os.path.join(self.vault, "Atlas", "Concepts", "Laplace tricks.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\nmore\n")

        def changed(system, user):
            data = json.loads(fake_cards([])(system, user))
            for c in data["cards"]:
                c["back"] = "A better answer"
            return json.dumps(data)
        cards.run(self.paths, self.ss_module, calls={"local": changed}, today=self.today, say=self.out)
        out3 = self.sync()
        per_note = sum(k["count"] for k in CFG["cards"]["kinds"].values())
        self.assertEqual(out3["synced"]["signals-and-systems"]["updated"], per_note)
        self.assertEqual(len(self.fake.notes), n)
        self.assertTrue(os.path.exists(out3["tsv"][0]))

    def test_review_stats_weak_topics_progress_and_learner(self):
        self.sync()
        self.fake.review("Laplace transform", lapses=3, factor=1900)
        self.fake.review("Fourier transform", lapses=0, factor=2600, due=False)
        out = self.sync()
        stats = out["stats"]["signals-and-systems"]
        self.assertEqual(stats["weak"], ["Laplace transform"])
        lap = next(t for t in stats["topics"] if t["topic"] == "Laplace transform")
        self.assertEqual(lap["ease"], 190)
        self.assertGreater(lap["due"], 0)
        self.assertIn(5, lap["weeks"])
        saved = json.loads(self.read(self.paths.data(CFG["don"]["anki_file"].format(slug="signals-and-systems"))))
        self.assertEqual(saved["weak"], ["Laplace transform"])
        progress = self.read(don.progress_path(self.paths, self.ss_module))
        self.assertIn("## Weak topics", progress)
        self.assertIn("- **Laplace transform**: revise [[Signals and Systems Week 5 tutor|Week 5]]", progress)
        # The learner model: a measured belief, with the numbers as evidence.
        model = json.loads(self.read(os.path.join(self.vault, "Dev", "learner", "model.json")))
        weak = [b for b in model["beliefs"] if b["status"] == "current"]
        self.assertEqual(len(weak), 1)
        self.assertEqual(weak[0]["source"], "measured")
        self.assertEqual(weak[0]["area"], CFG["learner"]["area"])
        self.assertIn("Laplace transform", weak[0]["claim"])
        self.assertIn("lapse", weak[0]["evidence"])
        self.assertTrue(os.path.exists(os.path.join(self.vault, "Atlas", "Concepts", "Learning", "Learner-Model.md")))
        # Same stats again: no second belief.
        self.sync()
        model = json.loads(self.read(os.path.join(self.vault, "Dev", "learner", "model.json")))
        self.assertEqual(len(model["beliefs"]), 1)
        # He gets it now: the belief is superseded, not deleted.
        self.fake.review("Laplace transform", lapses=0, factor=2600, reps=6, due=False)
        self.sync()
        model = json.loads(self.read(os.path.join(self.vault, "Dev", "learner", "model.json")))
        self.assertEqual([b["status"] for b in model["beliefs"]], ["superseded", "current"])
        self.assertIn("Keeps", model["beliefs"][1]["claim"])

    def test_cli_cards_and_anki_sync(self):
        out = io.StringIO()
        log = []
        with unittest.mock.patch("sys.stdout", out):
            rc = cli.main(["cards", "signals", "--vault", self.vault, "--state", self.state],
                          calls={"local": fake_cards(log)})
            self.assertEqual(rc, 0)
            rc = cli.main(["anki-sync", "--vault", self.vault, "--state", self.state], anki_client=self.client)
            self.assertEqual(rc, 0)
            rc = cli.main(["cards", "basket weaving", "--vault", self.vault, "--state", self.state])
            self.assertEqual(rc, 1)
        said = out.getvalue()
        self.assertIn("anki tsv", said)
        self.assertIn("added", said)
        self.assertIn("vault commit skipped", said)  # the fixture vault isn't a git repo


class MigrationTests(DonFixture):
    def test_old_state_is_read_then_moved_once(self):
        self.run_ingest()
        # Pretend the foundation's run wrote to the old place: move the sidecars back there.
        os.makedirs(self.state, exist_ok=True)
        for name in os.listdir(self.paths.data_dir):
            shutil.move(self.paths.data(name), os.path.join(self.state, name))
        self.assertFalse(os.path.exists(self.paths.state_file()))
        # Still read from the old place: a second ingest knows every file.
        result, log, _ = self.run_ingest()
        self.assertEqual(log, [])
        self.assertEqual(len(result["unchanged"]), 3)
        self.assertEqual(self.paths.readable("topics-signals-and-systems.json"),
                         os.path.join(self.state, "topics-signals-and-systems.json"))
        os.remove(self.paths.state_file())  # the ingest above saved a vault copy; undo that to test migrate
        moved = don.migrate(self.paths, say=self.out)
        self.assertIn(self.paths.state_file(), moved)
        self.assertIn(self.paths.data("topics-signals-and-systems.json"), moved)
        self.assertTrue(os.path.exists(os.path.join(self.state, "ingest.json")))  # old copy left
        self.assertEqual(don.migrate(self.paths, say=self.out), [])  # one-time
        self.assertEqual(self.paths.readable("topics-signals-and-systems.json"),
                         self.paths.data("topics-signals-and-systems.json"))


class GitTests(DonFixture):
    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.vault, capture_output=True, text=True, check=True).stdout

    def test_every_write_committed_only_its_files(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        # Something of his, unstaged and staged, that must not ride along.
        with open(os.path.join(self.vault, ".obsidian", "workspace.md"), "a", encoding="utf-8") as f:
            f.write("changed\n")
        with open(os.path.join(self.vault, "Atlas", "Concepts", "Unrelated.md"), "a", encoding="utf-8") as f:
            f.write("his edit\n")
        self.git("add", "Atlas/Concepts/Unrelated.md")
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = cli.main(["cards", "signals", "--vault", self.vault, "--state", self.state],
                          calls={"local": fake_cards([])})
        self.assertEqual(rc, 0)
        self.assertIn("vault commit", out.getvalue())
        msg = self.git("log", "-1", "--format=%s")
        self.assertEqual(msg.strip(), CFG["git"]["messages"]["cards"].format(tutor=conf.tutor_name(),
                                                                              module="Signals and Systems"))
        files = set(self.git("show", "--name-only", "--format=", "HEAD").split("\n")) - {""}
        don_rel = "/".join(CFG["don"]["folder"]) + "/"
        self.assertTrue(files)
        self.assertTrue(all(f.startswith(don_rel) for f in files), files)
        self.assertIn("Atlas/Concepts/Unrelated.md", self.git("diff", "--cached", "--name-only"))
        self.assertIn(".obsidian/workspace.md", self.git("status", "--porcelain"))


class NoNameLiteralTests(unittest.TestCase):
    def test_tutor_name_is_not_in_code(self):
        import ast
        name = conf.tutor_name()
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
            for bad in (name, "8765", "Front", "Anki", "http://"):
                self.assertFalse([s for s in literals if bad in s], (fn, bad))


if __name__ == "__main__":
    unittest.main()
