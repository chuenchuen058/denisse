"""python -m unittest atrium.test_tokens -v

The tokens collector (atrium/usage_ledger.py) and the card it feeds: small fixture Claude Code and Codex
logs in a temp folder, a temp .review for the ledger, a temp config pointing at them. Each
Claude Code message counts once; nothing before all_from counts; the ledger keeps a day whose log
was deleted; Orchestra's own Claude runs count once, from their log when there is one, else from
state.json; logs are read from where the last read stopped. No real logs, no network."""
import copy
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atrium import board, snapshot, usage_ledger as tokens

SHIPPED = json.loads((Path(__file__).with_name("config.json")).read_text(encoding="utf-8-sig"))
NOW = dt.datetime(2026, 11, 13, 12, 0, 0)


def cc(mid, day, session="s-chuen", i=10, o=20, cr=300, cw=40):
    """One Claude Code assistant line."""
    return json.dumps({"type": "assistant", "sessionId": session, "timestamp": f"{day}T10:00:00",
                       "message": {"id": mid, "role": "assistant",
                                   "usage": {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
                                             "cache_creation_input_tokens": cw}}})


def cx(day, inp, cached, out):
    """One Codex token_count event with the session's running total."""
    return json.dumps({"timestamp": f"{day}T09:00:00", "type": "event_msg",
                       "payload": {"type": "token_count", "info": {"total_token_usage": {
                           "input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
                           "output_tokens": out}}}})


class TokensBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.review = self.tmp / "review"
        self.review.mkdir()
        self.cc_root, self.cx_root = self.tmp / "claude" / "projects", self.tmp / "codex" / "sessions"
        cfg = copy.deepcopy(SHIPPED)
        t = cfg["live"]["board"]["tokens"]
        t["scan_s"] = 0
        for src in t["sources"]:
            if src["kind"] == "claude_code":
                src["root"] = str(self.cc_root)
            elif src["kind"] == "codex":
                src["root"] = str(self.cx_root)
        (self.tmp / "atrium.json").write_text(json.dumps(cfg), encoding="utf-8")
        for p in (mock.patch.dict(os.environ, {"ATRIUM_CONFIG": str(self.tmp / "atrium.json")}),
                  mock.patch.object(snapshot, "REVIEW", self.review),
                  mock.patch.dict(tokens._mem, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def log(self, root: Path, rel: str, *lines: str, end: str = "\n") -> Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + end)
        return p

    def days(self, state=None, save=True):
        return tokens.update(state or {}, NOW, save=save)["days"]


class Collector(TokensBase):
    def test_claude_code_message_counts_once(self):
        # the same id once per content block, and again in a resumed session's file
        self.log(self.cc_root, "C--home/a.jsonl", cc("m1", "2026-10-12"), cc("m1", "2026-10-12"), cc("m2", "2026-10-12"))
        self.log(self.cc_root, "C--home/b.jsonl", cc("m1", "2026-10-12"))
        got = self.days()["2026-10-12"]["claude"]
        self.assertEqual(got, {"in": 20, "out": 40, "cache_read": 600, "cache_write": 80})

    def test_nothing_before_all_from(self):
        self.log(self.cc_root, "C--home/a.jsonl", cc("old", "2026-08-31"), cc("new", "2026-09-01"))
        self.log(self.cx_root, "2026/08/31/r.jsonl", cx("2026-08-31", 100, 50, 5))
        days = self.days()
        self.assertNotIn("2026-08-31", days)
        self.assertEqual(days["2026-09-01"]["claude"]["in"], 10)

    def test_ledger_keeps_a_day_whose_log_was_deleted(self):
        p = self.log(self.cc_root, "C--home/a.jsonl", cc("m1", "2026-10-12"))
        before = self.days()
        self.assertTrue((self.review / SHIPPED["live"]["board"]["tokens"]["ledger"]).exists())
        p.unlink()
        tokens._mem.clear()  # a fresh process: from the ledger on disk alone
        self.assertEqual(self.days(), before)

    def _orchestra_fixture(self):
        state = {"tasks": {"t": {"history": [{"end": "2026-11-12T10:00:00", "lane": "claude", "session_id": "s-orch",
                                              "tokens": {"in": 1, "out": 2, "cache_read": 3, "cache_write": 4}}]}}}
        # its session in an ordinary project folder (matched by session id) ...
        self.log(self.cc_root, "C--home/orch.jsonl", cc("o1", "2026-11-12", session="s-orch"))
        # ... and an unrecorded run in a worktree project
        self.log(self.cc_root, "C--Users-x-GitHub-denisse-wt-2026-11-12T0900-task/w.jsonl",
                 cc("w1", "2026-11-12", session="s-killed"))
        self.log(self.cc_root, "C--home/mine.jsonl", cc("c1", "2026-11-12"))
        return state

    def test_the_logs_win_over_state_json_for_claude_code_runs(self):
        # 15 Nov 2026: state.json recorded 0.61 Bill where the worktree logs held ~0.9B. With
        # prefer_logs (shipped) every log counts, worktrees too, and the run its log holds is
        # not counted again from state.json
        got = self.days(self._orchestra_fixture())["2026-11-12"]["claude"]
        self.assertEqual(got, {"in": 30, "out": 60, "cache_read": 900, "cache_write": 120})

    def test_a_run_with_no_log_still_counts_from_state_json(self):
        state = {"tasks": {"t": {"history": [
            {"end": "2026-11-12T10:00:00", "lane": "claude", "session_id": "s-gone",
             "tokens": {"in": 1, "out": 2, "cache_read": 3, "cache_write": 4}},
            {"end": "2026-11-12T11:00:00", "lane": "claude",
             "tokens": {"in": 100, "out": 200, "cache_read": 300, "cache_write": 400}}]}}}
        self.log(self.cc_root, "C--home/mine.jsonl", cc("c1", "2026-11-12"))
        got = self.days(state)["2026-11-12"]["claude"]
        self.assertEqual(got, {"in": 111, "out": 222, "cache_read": 603, "cache_write": 444})

    def test_a_logged_run_stays_out_after_its_log_is_pruned(self):
        state = self._orchestra_fixture()
        before = self.days(state)
        shutil.rmtree(self.cc_root)  # Claude Code prunes its old logs
        tokens._mem.clear()
        self.assertEqual(self.days(state), before)  # the ledger kept the log's total and its session

    def test_an_older_state_json_total_for_a_day_is_replaced(self):
        # a ledger written the old way holds state.json's total for the day; the logs now cover it
        state = self._orchestra_fixture()
        ledger = self.review / SHIPPED["live"]["board"]["tokens"]["ledger"]
        ledger.write_text(json.dumps({"orchestra": {"2026-11-12": {"claude": {"in": 1, "out": 2, "cache_read": 3,
                                                                               "cache_write": 4}}}}), encoding="utf-8")
        self.assertEqual(self.days(state)["2026-11-12"]["claude"]["in"], 30)

    def test_without_prefer_logs_orchestra_runs_are_not_counted_again_from_the_logs(self):
        cfg = json.loads((self.tmp / "atrium.json").read_text(encoding="utf-8"))
        for src in cfg["live"]["board"]["tokens"]["sources"]:
            src.pop("prefer_logs", None)
        (self.tmp / "atrium.json").write_text(json.dumps(cfg), encoding="utf-8")
        got = self.days(self._orchestra_fixture())["2026-11-12"]["claude"]
        self.assertEqual(got, {"in": 11, "out": 22, "cache_read": 303, "cache_write": 44})

    def test_logs_read_from_where_they_stopped(self):
        p = self.log(self.cc_root, "C--home/a.jsonl", cc("m1", "2026-10-12"))
        self.log(self.cc_root, "C--home/a.jsonl", cc("m2", "2026-10-12"), end="")  # still being written
        self.assertEqual(self.days()["2026-10-12"]["claude"]["in"], 10)
        mark = json.loads((self.review / SHIPPED["live"]["board"]["tokens"]["ledger"]).read_text())["files"][str(p)]
        self.assertLess(mark["offset"], p.stat().st_size)
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n")
        self.assertEqual(self.days()["2026-10-12"]["claude"]["in"], 20)
        self.assertEqual(self.days()["2026-10-12"]["claude"]["in"], 20)  # nothing new: nothing added

    def test_codex_adds_the_running_total_difference(self):
        p = self.log(self.cx_root, "2026/11/04/r.jsonl", cx("2026-11-04", 100, 60, 10), cx("2026-11-04", 100, 60, 10),
                     cx("2026-11-04", 250, 160, 30))
        self.assertEqual(self.days()["2026-11-04"]["codex"],
                         {"in": 90, "out": 30, "cache_read": 160, "cache_write": 0})
        self.log(self.cx_root, "2026/11/04/r.jsonl", cx("2026-11-05", 300, 200, 31))
        days = self.days()
        self.assertEqual(days["2026-11-05"]["codex"], {"in": 10, "out": 1, "cache_read": 40, "cache_write": 0})
        self.assertEqual(days["2026-11-04"]["codex"]["in"], 90)
        self.assertTrue(p.exists())

    def test_the_live_page_reads_but_never_writes_the_ledger(self):
        self.log(self.cc_root, "C--home/a.jsonl", cc("m1", "2026-10-12"))
        self.assertEqual(self.days(save=False)["2026-10-12"]["claude"]["in"], 10)
        self.assertEqual(list(self.review.iterdir()), [])
        self.log(self.cc_root, "C--home/a.jsonl", cc("m2", "2026-10-12"))
        self.assertEqual(self.days(save=False)["2026-10-12"]["claude"]["in"], 20)  # carried on in memory
        self.assertEqual(list(self.review.iterdir()), [])

    def test_bad_lines_are_skipped(self):
        self.log(self.cc_root, "C--home/a.jsonl", '{"usage": broken', cc("m1", "2026-10-12"))
        self.assertEqual(self.days()["2026-10-12"]["claude"]["out"], 20)


try:
    import zstandard
except ImportError:  # requirements.txt lists it; the .gz tests still cover the stream reader
    zstandard = None


class Compressed(TokensBase):
    """Codex compresses older rollouts to .jsonl.zst (Oct 2026: 224 Mil tokens the card missed)."""

    def pack(self, plain: Path, suffix: str, remove=True) -> Path:
        data = plain.read_bytes()
        out = plain.with_name(plain.name + suffix)
        if suffix == ".zst":
            out.write_bytes(zstandard.ZstdCompressor().compress(data))
        else:
            import gzip
            out.write_bytes(gzip.compress(data))
        if remove:
            plain.unlink()
        return out

    def use_gz(self):
        cfg = json.loads((self.tmp / "atrium.json").read_text(encoding="utf-8"))
        for src in cfg["live"]["board"]["tokens"]["sources"]:
            if src["kind"] == "codex":
                src["compressed"] = [".zst", ".gz"]
        (self.tmp / "atrium.json").write_text(json.dumps(cfg), encoding="utf-8")

    def rollout(self, rel="2026/10/15/rollout-2026-10-15T09-00-00-abc.jsonl"):
        return self.log(self.cx_root, rel, cx("2026-10-15", 100, 60, 10), cx("2026-10-15", 250, 160, 30))

    @unittest.skipUnless(zstandard, "zstandard not installed")
    def test_a_zst_rollout_is_read(self):
        self.pack(self.rollout(), ".zst")
        self.assertEqual(self.days()["2026-10-15"]["codex"], {"in": 90, "out": 30, "cache_read": 160, "cache_write": 0})
        self.assertEqual(self.days()["2026-10-15"]["codex"]["in"], 90)  # read once, not again each scan

    def test_a_gz_rollout_is_read_when_listed(self):
        self.use_gz()
        p = self.rollout()
        self.log(self.cx_root, p.relative_to(self.cx_root).as_posix(), cx("2026-10-16", 300, 200, 31), end="")
        self.pack(p, ".gz")  # the last line has no newline: a finished archive, so it counts
        days = self.days()
        self.assertEqual(days["2026-10-15"]["codex"]["in"], 90)
        self.assertEqual(days["2026-10-16"]["codex"], {"in": 10, "out": 1, "cache_read": 40, "cache_write": 0})

    def test_gz_is_not_read_unless_listed(self):
        self.pack(self.rollout(), ".gz")
        self.assertNotIn("2026-10-15", self.days())

    @unittest.skipUnless(zstandard, "zstandard not installed")
    def test_a_log_compressed_after_it_was_read_is_not_counted_twice(self):
        p = self.rollout()
        self.assertEqual(self.days()["2026-10-15"]["codex"]["in"], 90)
        self.pack(p, ".zst")
        tokens._mem.clear()  # a later refresh, from the ledger on disk
        self.assertEqual(self.days()["2026-10-15"]["codex"]["in"], 90)

    @unittest.skipUnless(zstandard, "zstandard not installed")
    def test_both_copies_at_once_count_once(self):
        self.pack(self.rollout(), ".zst", remove=False)
        self.assertEqual(self.days()["2026-10-15"]["codex"]["in"], 90)

    def test_lines_added_before_compression_are_picked_up(self):
        self.use_gz()
        p = self.rollout()
        self.days()
        self.log(self.cx_root, p.relative_to(self.cx_root).as_posix(), cx("2026-10-16", 300, 200, 31))
        self.pack(p, ".gz")
        days = self.days()
        self.assertEqual(days["2026-10-15"]["codex"]["in"], 90)
        self.assertEqual(days["2026-10-16"]["codex"]["in"], 10)

    def test_missing_zstandard_says_so_on_the_card(self):
        p = self.rollout()
        p.with_name(p.name + ".zst").write_bytes(b"not read")
        p.unlink()
        real_import = __import__

        def no_zstd(name, *a, **k):
            if name == "zstandard":
                raise ImportError(name)
            return real_import(name, *a, **k)

        with mock.patch("builtins.__import__", no_zstd):
            got = tokens.update({}, NOW, save=True)
            self.assertNotIn("2026-10-15", got["days"])
            note = "1 compressed Codex logs not read (install zstandard)"
            self.assertEqual(got["notes"], [note])
            x = {"words": SHIPPED["live"]["board"]["words"], "now": NOW, "state": {"tasks": {"t": {"history": [
                {"end": "2026-11-13T09:00:00", "lane": "claude", "tokens": {"in": 1, "out": 0, "cache_read": 0,
                                                                            "cache_write": 0}}]}}},
                 "agents": [{"lane": "codex", "label": "Codex"}]}
            self.assertIn(note, board._tokens(x))


class Card(TokensBase):
    def test_all_time_cell_and_per_agent_line(self):
        self.log(self.cc_root, "C--home/a.jsonl", cc("m1", "2026-10-12", i=1000, o=0, cr=0, cw=0))
        self.log(self.cx_root, "2026/11/13/r.jsonl", cx("2026-11-13", 2500, 500, 0))
        state = {"tasks": {"t": {"history": [{"end": "2026-11-13T09:00:00", "lane": "claude", "session_id": "s",
                                              "tokens": {"in": 0, "out": 0, "cache_read": 3000, "cache_write": 0}}]}}}
        x = {"words": SHIPPED["live"]["board"]["words"], "now": NOW, "state": state,
             "agents": [{"lane": "claude", "label": "Claude"}, {"lane": "codex", "label": "Codex"},
                        {"lane": "copilot", "label": "Copilot"}]}
        html = board._tokens(x)
        self.assertIn("all time · since 01 Sep (data from 12 Oct)", html)
        self.assertIn("Claude 4.0k", html)
        self.assertIn("Codex 2.5k", html)
        self.assertIn("Copilot n/a", html)
        self.assertIn('<div class="big mid">6.5k</div>', html)  # all time
        self.assertIn('<div class="big mid">5.5k</div>', html)  # today


if __name__ == "__main__":
    unittest.main()
