"""Two runner processes saving state: neither loses the other's history entry for the same task."""
import contextlib
import copy
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ORCHESTRA_POWER", "ac")
import runner  # noqa: E402


def blank():
    return {"tasks": {}, "done": {}, "lanes": {}, "durations": {}, "days": {}}


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.base = blank()
        self.base["tasks"]["t"] = {"attempts": 1, "history": [{"end": "2026-11-05T10:00:00", "seconds": 5}]}
        self.base["days"]["2026-11-05"] = {"work_s": 5, "cost_usd": 0.5, "known_cost_s": 5}

    def run_in(self, end, seconds, cost):
        s = copy.deepcopy(self.base)
        s["tasks"]["t"]["history"].append({"end": end, "seconds": seconds})
        s["tasks"]["t"]["attempts"] += 1
        d = s["days"]["2026-11-05"]
        d["work_s"] += seconds
        d["cost_usd"] += cost
        d["known_cost_s"] += seconds
        return s

    def test_both_appended_to_the_same_task(self):
        theirs = self.run_in("2026-11-05T10:05:00", 7, 0.7)   # saved first by another process
        ours = self.run_in("2026-11-05T10:06:00", 9, 0.9)
        out = runner._merge_into(copy.deepcopy(theirs), ours, self.base)
        hist = out["tasks"]["t"]["history"]
        self.assertEqual([h["seconds"] for h in hist], [5, 7, 9])
        self.assertEqual(sum(h["seconds"] for h in hist), out["days"]["2026-11-05"]["work_s"])

    def test_only_we_changed_it(self):
        ours = self.run_in("2026-11-05T10:06:00", 9, 0.9)
        out = runner._merge_into(copy.deepcopy(self.base), ours, self.base)
        self.assertEqual(out["tasks"]["t"], ours["tasks"]["t"])

    def test_a_field_only_they_changed_is_kept(self):
        theirs = copy.deepcopy(self.base)
        theirs["tasks"]["t"].update(merge="merged", integration={"commit": "abc123"})
        ours = copy.deepcopy(self.base)
        ours["tasks"]["t"]["last_result"] = "PASS"
        out = runner._merge_into(copy.deepcopy(theirs), ours, self.base)
        t = out["tasks"]["t"]
        self.assertEqual((t.get("merge"), t.get("integration"), t.get("last_result")),
                         ("merged", {"commit": "abc123"}, "PASS"))

    def test_their_new_entry_survives_our_edit_of_an_old_one(self):
        theirs = self.run_in("2026-11-05T10:05:00", 1, 0.1)
        ours = copy.deepcopy(self.base)
        ours["tasks"]["t"]["history"][0]["recovered"] = True
        out = runner._merge_into(copy.deepcopy(theirs), ours, self.base)
        hist = out["tasks"]["t"]["history"]
        self.assertEqual([h["seconds"] for h in hist], [5, 1])
        self.assertTrue(hist[0].get("recovered"))
        self.assertEqual(sum(h["seconds"] for h in hist), out["days"]["2026-11-05"]["work_s"])

    def test_the_same_run_collected_twice_shows_twice(self):
        """Both processes appended an identical entry since base: both charged days[], so history
        keeps both and the sums agree (dropping one left days 1 s over history on CI, 6 Nov)."""
        theirs = self.run_in("2026-11-05T10:05:00", 7, 0.7)
        ours = self.run_in("2026-11-05T10:05:00", 7, 0.7)
        ours["tasks"]["t"]["last_result"] = "PASS"
        out = runner._merge_into(copy.deepcopy(theirs), ours, self.base)
        hist = out["tasks"]["t"]["history"]
        self.assertEqual(len(hist), 3)
        self.assertEqual(sum(h["seconds"] for h in hist), out["days"]["2026-11-05"]["work_s"])
        self.assertEqual(out["tasks"]["t"]["last_result"], "PASS")


class UnreadableStateTest(unittest.TestCase):
    """A state.json that isn't JSON used to be dropped without a word, and the next save wrote
    over it: the day's spend, the attempt counts and the done set were gone with no trace."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for name, value in (("REVIEW", self.dir), ("STATE_FILE", self.dir / "state.json"),
                            ("LOG_FILE", self.dir / "runner.log")):
            p = mock.patch.object(runner, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_it_is_kept_aside_and_logged_before_a_save_can_replace_it(self):
        bad = '{"days": {"2026-11-10": {"cost_usd": 4.2'
        runner.STATE_FILE.write_text(bad, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            st = runner.load_state()
            self.assertEqual(st["tasks"], {})  # the tick still runs, from an empty state
            runner.save_state(st)
        kept = [p for p in self.dir.iterdir() if ".corrupt-" in p.name]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding="utf-8"), bad)
        self.assertIn("STATE UNREADABLE", runner.LOG_FILE.read_text(encoding="utf-8"))
        self.assertIn(kept[0].name, runner.LOG_FILE.read_text(encoding="utf-8"))

    def test_a_missing_or_good_file_keeps_nothing(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.load_state()["done"], {})
            runner.STATE_FILE.write_text('{"done": {"abc": "2026-11-10T00:00:00"}}', encoding="utf-8")
            self.assertEqual(runner.load_state()["done"], {"abc": "2026-11-10T00:00:00"})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["state.json"])

    def test_json_that_is_not_an_object_is_an_empty_state_not_a_crash(self):
        runner.STATE_FILE.write_text("[]", encoding="utf-8")
        self.assertEqual(runner.load_state()["tasks"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
