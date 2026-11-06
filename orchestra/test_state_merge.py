"""Two runner processes saving state: neither loses the other's history entry for the same task."""
import copy
import os
import sys
import unittest

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

    def test_same_entry_is_not_doubled(self):
        both = self.run_in("2026-11-05T10:05:00", 7, 0.7)
        ours = copy.deepcopy(both)
        ours["tasks"]["t"]["last_result"] = "PASS"
        out = runner._merge_into(copy.deepcopy(both), ours, self.base)
        self.assertEqual(len(out["tasks"]["t"]["history"]), 2)
        self.assertEqual(out["tasks"]["t"]["last_result"], "PASS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
