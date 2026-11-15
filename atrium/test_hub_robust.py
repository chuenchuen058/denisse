"""python -m unittest atrium.test_hub_robust -v

The live hub's back-end pass (15 Nov 2026): a missing or broken state.json, token ledger or
hot.md says "can't read X" on the cards that need it and nowhere else, never a blank page or a
500; a card or table that fails costs only itself; the page's reads are cached on what the
file is (and still see a change); the Markets card reads the served .review; the token scan
can run behind the first page. Temp .review and vault, fixed clock, no network, no Ollama."""
import json
import os
import threading
import time
from unittest import mock

from atrium import board, conf, usage_ledger
from atrium.test_board import BOARD, BoardBase, fixture, section
from atrium.test_live import SHIPPED, w
from atrium.test_tokens import cc

SHIPPED_LIVE = SHIPPED["live"]
WORDS = BOARD["words"]
WHY = BOARD["source_why"]
STATE, LEDGER, HOT = (next(s for s in BOARD["sources"] if s["path"] == p) for p in ("state.json", "{ledger}", "hot.md"))
CARD_KEYS = {"lanes": [f'lane:{a["lane"]}' for a in SHIPPED_LIVE["agents"]]}


def cant(src: dict, why: str) -> str:
    return WORDS["cant_read"].format(name=src["name"], why=WHY[why])


def keys(card: str) -> list[str]:
    return CARD_KEYS.get(card, [card])


class SourcesTest(BoardBase):
    """Each source broken one way at a time, on top of the full fixture."""

    def setUp(self):
        super().setUp()
        fixture(self.review, self.vault, self.now)

    def esc(self, text: str) -> str:
        return board.e(text)

    def assert_only_on_its_cards(self, src: dict, why: str):
        page = self.page()
        line = self.esc(cant(src, why))
        on = {k for c in src["cards"] for k in keys(c)}
        for c in BOARD["cards"]:
            for k in keys(c["id"]):
                body = section(page, k)
                if not body:
                    continue
                (self.assertIn if k in on else self.assertNotIn)(line, body, k)
        self.assertIn('id="grid"', page)
        return page

    def test_missing_state_json(self):
        (self.review / "state.json").unlink()
        self.assert_only_on_its_cards(STATE, "missing")

    def test_corrupt_state_json(self):
        w(self.review, "state.json", '{"tasks": {"t": ')
        self.assert_only_on_its_cards(STATE, "bad_json")

    def test_state_json_that_is_not_an_object(self):
        w(self.review, "state.json", "[1, 2, 3]")
        page = self.assert_only_on_its_cards(STATE, "not_object")
        self.assertIn("Heron nest survey", page)  # the queue and running task still show

    def test_corrupt_ledger(self):
        w(self.review, BOARD["tokens"]["ledger"], "{half a ledger")
        self.assert_only_on_its_cards(LEDGER, "bad_json")

    def test_missing_hot_md(self):
        (self.vault / "hot.md").unlink()
        self.assert_only_on_its_cards(HOT, "missing")

    def test_hot_md_unreadable(self):
        (self.vault / "hot.md").unlink()
        (self.vault / "hot.md").mkdir()  # a folder where the note should be
        self.assert_only_on_its_cards(HOT, "unreadable")

    def test_a_fixed_file_clears_the_line(self):
        good = (self.review / "state.json").read_text(encoding="utf-8")
        w(self.review, "state.json", "{not json")
        self.assertIn(self.esc(cant(STATE, "bad_json")), self.page())
        w(self.review, "state.json", good)
        self.assertNotIn(self.esc(cant(STATE, "bad_json")), self.page())

    def test_all_three_broken_is_still_served_as_json(self):
        w(self.review, "state.json", "{not json")
        w(self.review, BOARD["tokens"]["ledger"], "[]")
        (self.vault / "hot.md").unlink()
        srv = self.serve()
        code, ctype, body = self.get(srv, "/data")
        self.assertEqual(code, 200)
        cards = {c["key"]: c["html"] for c in json.loads(body)["board"]["cards"]}
        self.assertIn(self.esc(cant(STATE, "bad_json")), cards["limits"])
        self.assertIn(self.esc(cant(LEDGER, "not_object")), cards["tokens"])
        self.assertIn(self.esc(cant(HOT, "missing")), cards["projects"])


class FailuresTest(BoardBase):
    """A bug in one card or one table: that card says so, the others render."""

    def setUp(self):
        super().setUp()
        fixture(self.review, self.vault, self.now)

    def test_a_card_that_raises_says_so_and_the_rest_render(self):
        with mock.patch.object(board, "_limits", side_effect=KeyError("boom")):
            page = self.page()
        self.assertIn(board.e(WORDS["cant_build"].format(why="KeyError")), section(page, "limits"))
        self.assertIn("Kestrel box build", section(page, "projects"))
        self.assertNotIn(board.e(WORDS["cant_build"].format(why="KeyError")), section(page, "projects"))

    def test_a_table_that_raises_is_named_on_its_cards(self):
        with mock.patch.object(self.snap, "_usage", side_effect=ValueError("bad row")):
            d = self.live.collect()
        line = board.e(WORDS["cant_table"].format(table="usage", why="ValueError"))
        cards = {c["key"]: c["html"] for c in d["board"]["cards"]}
        for card in BOARD["table_cards"]["usage"]:
            for k in keys(card):
                self.assertIn(line, cards[k], k)
        self.assertNotIn(line, cards["projects"])
        json.dumps(d)

    def test_the_hub_keeps_its_last_good_copy(self):
        hub = self.live.Hub()
        v, good = hub.get()
        w(self.review, "runner.log", "changed\n")  # a new fingerprint: it rebuilds
        with mock.patch.object(self.live, "collect", side_effect=RuntimeError("bug")):
            self.assertEqual(hub.get(), (v, good))

    def test_nothing_built_yet_is_a_503_with_the_reason(self):
        srv = self.serve()
        with mock.patch.object(self.live, "collect", side_effect=RuntimeError("bug")):
            code, ctype, body = self.get(srv, "/data")
        self.assertEqual(code, 503)
        self.assertEqual(body.decode("utf-8"), WORDS["cant_build"].format(why="RuntimeError"))
        code, _, _ = self.get(srv, "/data")  # and it recovers once the bug is gone
        self.assertEqual(code, 200)


class CachingTest(BoardBase):
    def setUp(self):
        super().setUp()
        fixture(self.review, self.vault, self.now)

    def test_config_is_read_once_per_build(self):
        real = conf._read
        with mock.patch.object(conf, "_read", side_effect=real) as read:
            self.live.collect()
        self.assertEqual(read.call_count, 1)
        self.assertIsNone(getattr(conf._held, "data", None))  # and fresh again after it
        cfg = json.loads((self.tmp / "atrium.json").read_text(encoding="utf-8"))
        cfg["live"]["title"] = "Changed title"
        (self.tmp / "atrium.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertEqual(self.live.collect()["title"], "Changed title")

    def test_held_is_per_thread(self):
        seen = {}
        with conf.held():
            mine = conf.load()
            t = threading.Thread(target=lambda: seen.setdefault("other", conf.load()))
            t.start(), t.join()
            self.assertIs(conf.load(), mine)
        self.assertIsNot(seen["other"], mine)

    def test_task_files_are_read_again_only_when_they_change(self):
        self.live.collect()
        q = self.review / "queue" / "t-next.md"
        with mock.patch("pathlib.Path.read_text", side_effect=AssertionError("re-read")):
            self.snap._title(q, "")  # unchanged: from the cache
        w(self.review, "queue/t-next.md", "# Otter holt census, renamed\nQueue: auto\n")
        self.assertIn("Otter holt census, renamed", [t["task"] for t in self.snap.tables(["tasks"])["tasks"]])

    def test_a_same_size_rewrite_of_state_json_is_seen(self):
        st = json.loads((self.review / "state.json").read_text(encoding="utf-8"))
        self.live.collect()
        st["days"][self.now.date().isoformat()]["cost_usd"] = 99.9  # 17.4 -> 99.9: same length
        before = os.stat(self.review / "state.json")
        w(self.review, "state.json", json.dumps(st))
        os.utime(self.review / "state.json", ns=(before.st_atime_ns, before.st_mtime_ns))  # same mtime too
        self.assertEqual(self.snap.tables(["spend"])["spend"][0]["cost_usd"], 99.9)

    def test_hot_md_change_is_seen(self):
        self.live.collect()
        w(self.vault, "hot.md", "## Active right now\n- [Chuen] Wren survey\n")
        self.assertIn("Wren survey", section(self.page(), "projects"))

    def test_markets_come_from_the_served_review(self):
        w(self.review, f'hub/{SHIPPED_LIVE["markets_file"]}',
          json.dumps({"tickers": [{"ticker": "OWL", "price": "12.00", "day_change_pct": 1.5}]}))
        self.assertEqual([m["ticker"] for m in self.live.collect()["markets"]], ["OWL"])
        w(self.review, f'hub/{SHIPPED_LIVE["markets_file"]}', '{"tickers": "not a list"}')
        self.assertEqual(self.live.collect()["markets"], [])


class BackgroundCursorsTest(BoardBase):
    """live.serve() turns on background git for the cursors: a build never waits on git."""

    def test_git_runs_behind_the_page_and_lands(self):
        from atrium import cursors
        gate, rows = threading.Event(), [{"path": "atrium/live.py", "mtime": 1.0}]

        def slow(_wt):
            gate.wait(5)
            return rows
        wt = self.tmp / "wt"
        with mock.patch.dict(cursors._cache, clear=True), \
                mock.patch.dict(cursors._bg, {"on": True, "gen": 0, "busy": set()}), \
                mock.patch.object(cursors, "touched", side_effect=slow):
            t = time.monotonic()
            self.assertEqual(cursors.touched_cached(wt, 60), [])  # nothing yet, and no wait
            self.assertLess(time.monotonic() - t, 1)
            self.assertEqual(cursors.touched_cached(wt, 60), [])  # one re-read at a time
            gate.set()
            deadline = time.monotonic() + 5
            while cursors.generation() == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(cursors.generation(), 1)
            self.assertEqual(cursors.touched_cached(wt, 60), rows)
            self.assertEqual(cursors.touched.call_count, 1)


class BackgroundScanTest(BoardBase):
    """live.serve() turns on the background token scan: update() answers from the saved ledger
    at once, and the scan's result lands as a new generation."""

    def test_scan_runs_behind_and_lands(self):
        root = self.tmp / "logs" / "claude_code" / "C--home"
        root.mkdir(parents=True)
        day = "2026-10-12"  # after the shipped all-time start
        (root / "a.jsonl").write_text(cc("m1", day) + "\n", encoding="utf-8")
        gate = threading.Event()
        real = usage_ledger._scan

        def slow(*a):
            gate.wait(5)
            return real(*a)
        with mock.patch.dict(usage_ledger._mem, clear=True), \
                mock.patch.dict(usage_ledger._bg, {"on": True, "thread": None, "gen": 0}), \
                mock.patch.object(usage_ledger, "_scan", side_effect=slow):
            gen = usage_ledger.generation()
            first = usage_ledger.update({}, self.now)["days"]
            self.assertEqual(first, {})  # not held up by the scan
            gate.set()
            deadline = time.monotonic() + 5
            while usage_ledger.generation() == gen and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(usage_ledger.generation(), gen + 1)
            got = usage_ledger.update({}, self.now)["days"].get(day, {})
            self.assertEqual(got.get("claude", {}).get("in"), 10, "the scan's totals are served after it lands")
            self.assertFalse((self.review / BOARD["tokens"]["ledger"]).exists())  # the page never writes


if __name__ == "__main__":
    import unittest
    unittest.main()
