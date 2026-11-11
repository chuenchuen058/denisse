"""Tests for orchestra/lane_pick.py: pure functions on a fake config and fake live state
(no runner, no git, no lanes run). The lane names here are made up on purpose: the scorer must
not know any real lane."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import lane_pick as LP  # noqa: E402

NOW = dt.datetime(2026, 11, 11, 12, 0)


def cfg(**changes) -> dict:
    base = {
        "lane_auto_word": "auto",
        "lane_pick_yes_words": ["yes", "true", "1"],
        "lane_strengths": {
            "alpha": {"code-edit": 0.9, "writing": 0.4, "extract": 0.7, "debug": 0.9, "review": 0.9},
            "beta": {"code-edit": 0.5, "writing": 0.9, "extract": 0.7, "debug": 0.6, "review": 0.6},
            "gamma": {"code-edit": 0.3, "writing": 0.3, "extract": 0.3, "debug": 0.3, "review": 0.5},
        },
        "lane_strength_default": 0.1,
        "lane_preference_bonus": 1.0,
        "bugfix_lane": "alpha",
        "bugfix_lane_bonus": 1.5,
        "lane_pick_weights": {"fit": 1.0, "pacing": 0.5, "cost": 0.3, "busy": 0.2, "fallback_hint": 0.05},
        "lane_pick_explicit_scope": "hint",
        "lane_fallback": {"alpha": "beta", "beta": "gamma"},
        "lane_pacing": {},
        "unpriced_lanes": ["beta", "gamma"],
        "daily_spend_cap_usd": 5.0,
        "lane_usage_max_age_h": 24,
        "reviewer_override_lane": "reviewer",
    }
    base.update(changes)
    return base


def live(**changes) -> LP.Live:
    base = dict(now=NOW, lanes=["alpha", "beta", "gamma"], spend_left=5.0)
    base.update(changes)
    return LP.Live(**base)


def ask(asked="auto", work="code-edit", **kw) -> LP.Ask:
    return LP.Ask(slug="t", asked=asked, work=work, **kw)


class StrengthsTest(unittest.TestCase):
    def test_best_fit_lane_wins(self):
        self.assertEqual(LP.choose(ask(work="code-edit"), cfg(), live()).lane, "alpha")
        self.assertEqual(LP.choose(ask(work="writing"), cfg(), live()).lane, "beta")

    def test_unlisted_work_uses_the_default_strength(self):
        self.assertEqual(LP.fit("alpha", "no-such-kind", cfg()), 0.1)
        self.assertEqual(LP.fit("no-such-lane", "code-edit", cfg()), 0.1)

    def test_a_lane_missing_from_lanes_is_never_picked(self):
        c = cfg()
        c["lane_strengths"]["ghost"] = {"code-edit": 5.0}
        p = LP.choose(ask(), c, live())
        self.assertEqual(p.lane, "alpha")
        self.assertNotIn("ghost", [r["lane"] for r in p.scores])
        p = LP.choose(ask(asked="ghost"), cfg(lane_pick_explicit_scope="any"), live())
        self.assertNotEqual(p.lane, "ghost")


class AvailabilityTest(unittest.TestCase):
    def test_a_limited_lane_is_skipped(self):
        st = live(limited={"alpha": NOW + dt.timedelta(hours=2)})
        p = LP.choose(ask(work="code-edit"), cfg(), st)
        self.assertEqual(p.lane, "beta")
        row = next(r for r in p.scores if r["lane"] == "alpha")
        self.assertIn("limited until 14:00", row["unavailable"])

    def test_a_limit_in_the_past_is_no_limit(self):
        st = live(limited={"alpha": NOW - dt.timedelta(minutes=1)})
        self.assertEqual(LP.choose(ask(), cfg(), st).lane, "alpha")

    def test_nothing_available_says_so(self):
        until = NOW + dt.timedelta(hours=1)
        p = LP.choose(ask(), cfg(), live(limited={l: until for l in ("alpha", "beta", "gamma")}))
        self.assertFalse(p.available)
        self.assertIn("no lane available", p.why)

    def test_a_full_lane_loses_a_little(self):
        c = cfg()
        c["lane_strengths"]["beta"]["extract"] = 0.75  # beta just ahead of alpha
        self.assertEqual(LP.choose(ask(work="extract"), c, live()).lane, "beta")
        busy = live(busy={"beta": 1}, slots={"beta": 1})
        self.assertEqual(LP.choose(ask(work="extract"), c, busy).lane, "alpha")


class PacingTest(unittest.TestCase):
    def pacing_cfg(self, window=0, reserve=0.0):
        # beta resets every 7 days at Wed 11 Nov 2026 12:00 (so the next reset is a week after NOW)
        return cfg(lane_pacing={"beta": {"reset_anchor": "2026-11-04T12:00", "period_h": 168,
                                         "spend_window_h": window, "reserve": reserve}})

    def test_next_reset(self):
        self.assertEqual(LP.next_reset("2026-11-04T12:00", 168, NOW), dt.datetime(2026, 11, 18, 12, 0))
        self.assertEqual(LP.next_reset("2026-11-20T09:00", 168, NOW), dt.datetime(2026, 11, 13, 9, 0))
        self.assertIsNone(LP.next_reset("", 168, NOW))
        self.assertIsNone(LP.next_reset("2026-11-04T12:00", 0, NOW))

    def test_same_allowance_spent_near_the_reset_and_conserved_far_from_it(self):
        c = self.pacing_cfg()
        usage = lambda at: {"beta": {"left": 0.5, "at": at.isoformat()}}  # the same 50% left, seen just now
        n, f = dt.datetime(2026, 11, 18, 10, 0), dt.datetime(2026, 11, 11, 14, 0)
        near = live(now=n, usage=usage(n))   # 2 h before beta's reset
        far = live(now=f, usage=usage(f))    # 166 h before it
        near_score, near_hold, _ = LP.pacing("beta", c, near)
        far_score, far_hold, _ = LP.pacing("beta", c, far)
        self.assertGreater(near_score, 0)
        self.assertLess(far_score, 0)
        self.assertFalse(near_hold or far_hold)
        # alpha and beta fit extract equally: pacing decides
        self.assertEqual(LP.choose(ask(work="extract"), c, near).lane, "beta")
        self.assertEqual(LP.choose(ask(work="extract"), c, far).lane, "alpha")

    def test_use_it_or_lose_it_window_and_full_reserve(self):
        # reserve 1 = keep it all (the "hold Codex until its last hours" rule), spent in the window
        c = self.pacing_cfg(window=12, reserve=1.0)
        early = live(now=dt.datetime(2026, 11, 12, 12, 0))
        late = live(now=dt.datetime(2026, 11, 18, 4, 0))  # 8 h before the reset
        self.assertTrue(LP.pacing("beta", c, early)[1], "held early in the week, usage unknown")
        self.assertEqual(LP.pacing("beta", c, late)[:2], (1.0, False))
        self.assertEqual(LP.choose(ask(work="writing"), c, early).lane, "alpha")
        self.assertEqual(LP.choose(ask(work="writing"), c, late).lane, "beta")

    def test_at_the_reserve_a_lane_is_held_and_unknown_usage_is_not_used_up(self):
        c = self.pacing_cfg(reserve=0.2)
        at_reserve = live(usage={"beta": {"used": 0.85, "at": NOW.isoformat()}})
        self.assertTrue(LP.pacing("beta", c, at_reserve)[1])
        self.assertEqual(LP.choose(ask(work="writing"), c, at_reserve).lane, "alpha")
        unknown = live(usage={})
        self.assertEqual(LP.pacing("beta", c, unknown)[:2], (0.0, False))
        stale = live(usage={"beta": {"used": 0.99, "at": (NOW - dt.timedelta(days=3)).isoformat()}})
        self.assertEqual(LP.pacing("beta", c, stale)[:2], (0.0, False), "a stale record is unknown")
        self.assertEqual(LP.choose(ask(work="writing"), c, unknown).lane, "beta")

    def test_a_usage_record_can_carry_the_reset(self):
        c = self.pacing_cfg(window=12)
        usage = {"beta": {"left": 0.5, "reset": (NOW + dt.timedelta(hours=3)).isoformat()}}
        self.assertEqual(LP.pacing("beta", c, live(usage=usage))[0], 1.0)


class CostTest(unittest.TestCase):
    def test_priced_lane_over_the_spend_cap_is_skipped(self):
        p = LP.choose(ask(work="code-edit"), cfg(), live(spend_left=0.0))
        self.assertEqual(p.lane, "beta")
        row = next(r for r in p.scores if r["lane"] == "alpha")
        self.assertIn("spend cap $5.00 reached", row["unavailable"])

    def test_spend_weighs_on_priced_lanes_only(self):
        rows = {r["lane"]: r for r in LP.score(ask(), cfg(), live(spend_left=1.0))}
        self.assertAlmostEqual(rows["alpha"]["parts"]["cost"], -0.3 * 0.8)
        self.assertNotIn("cost", rows["beta"]["parts"], "unpriced lanes cost 0")
        c = cfg()
        c["lane_strengths"]["beta"]["code-edit"] = 0.8
        self.assertEqual(LP.choose(ask(), c, live(spend_left=5.0)).lane, "alpha")
        self.assertEqual(LP.choose(ask(), c, live(spend_left=0.5)).lane, "beta")


class BugfixTest(unittest.TestCase):
    def near_cap(self):
        return cfg(lane_pacing={"alpha": {"reset_anchor": "2026-11-04T12:00", "period_h": 168,
                                          "spend_window_h": 0, "reserve": 0.1}})

    def test_a_bug_fix_goes_to_the_bugfix_lane_by_weight(self):
        p = LP.choose(ask(asked="beta", work="debug", bugfix=True), cfg(), live())
        self.assertEqual((p.lane, p.moved), ("alpha", "bugfix"))
        self.assertIn("bugfix", next(r for r in p.scores if r["lane"] == "alpha")["parts"])

    def test_a_bug_fix_leaves_the_bugfix_lane_near_its_cap(self):
        st = live(usage={"alpha": {"used": 0.95, "at": NOW.isoformat()}})
        p = LP.choose(ask(work="debug", bugfix=True), self.near_cap(), st)
        self.assertEqual(p.lane, "beta")
        self.assertIn("reserve", next(r for r in p.scores if r["lane"] == "alpha")["unavailable"])
        p = LP.choose(ask(asked="beta", work="debug", bugfix=True), self.near_cap(), st)
        self.assertEqual((p.lane, p.moved), ("beta", ""), "explicit beta stays; alpha is at its reserve")
        roomy = live(usage={"alpha": {"used": 0.2, "at": NOW.isoformat()}})
        self.assertEqual(LP.choose(ask(work="debug", bugfix=True), self.near_cap(), roomy).lane, "alpha")


class ReviewerTest(unittest.TestCase):
    def test_the_reviewer_is_never_the_author(self):
        for author in ("alpha", "beta", "gamma"):
            p = LP.choose(ask(asked="", work="review", author=author), cfg(), live())
            self.assertTrue(p.available)
            self.assertNotEqual(p.lane, author)
        self.assertEqual(LP.choose(ask(asked="", work="review", author="beta"), cfg(), live()).lane, "alpha")

    def test_no_independent_reviewer_is_unavailable_not_the_author(self):
        st = live(limited={"beta": NOW + dt.timedelta(hours=1), "gamma": NOW + dt.timedelta(hours=1)})
        p = LP.choose(ask(asked="", work="review", author="alpha"), cfg(), st)
        self.assertFalse(p.available)
        p = LP.choose(ask(asked="", work="review", author="alpha"), cfg(), live(), pool=["alpha"])
        self.assertFalse(p.available)

    def test_a_reviewer_follows_pacing_and_lane_fixed_does_not_pin_it(self):
        c = cfg(lane_pacing={"alpha": {"reset_anchor": "2026-11-04T12:00", "period_h": 168,
                                       "spend_window_h": 0, "reserve": 0.0}})
        st = live(usage={"alpha": {"left": 0.05, "at": NOW.isoformat()}})
        p = LP.choose(ask(asked="", work="review", author="gamma", fixed=True), c, st)
        self.assertEqual(p.lane, "beta", "alpha is far behind its pace")


class HeaderTest(unittest.TestCase):
    def test_lane_fixed_is_honoured(self):
        st = live(limited={"gamma": NOW + dt.timedelta(hours=1)})
        p = LP.choose(ask(asked="gamma", fixed=True), cfg(), st)
        self.assertEqual(p.lane, "gamma")
        self.assertFalse(p.available)
        self.assertEqual([r["lane"] for r in p.scores], ["gamma"])
        p = LP.choose(ask(asked="gamma", fixed=True, bugfix=True), cfg(lane_pick_explicit_scope="any"), live())
        self.assertEqual(p.lane, "gamma", "neither the bug-fix bonus nor scope any moves it")

    def test_explicit_lane_respected_unless_unavailable(self):
        p = LP.choose(ask(asked="gamma", work="code-edit"), cfg(lane_pick_explicit_scope="any"), live())
        self.assertEqual((p.lane, p.moved), ("gamma", ""), "alpha fits better, but gamma was asked for")
        until = NOW + dt.timedelta(hours=1)
        p = LP.choose(ask(asked="gamma"), cfg(lane_pick_explicit_scope="any"), live(limited={"gamma": until}))
        self.assertEqual((p.lane, p.moved), ("alpha", "fallback"))
        # hint scope: only the lane_fallback hint may take it
        p = LP.choose(ask(asked="beta"), cfg(), live(limited={"beta": until}))
        self.assertEqual((p.lane, p.moved), ("gamma", "fallback"))
        p = LP.choose(ask(asked="beta"), cfg(lane_fallback={}), live(limited={"beta": until}))
        self.assertEqual((p.lane, p.available), ("beta", False), "no hint: it waits for its lane")

    def test_explicit_lane_ignores_pacing_hold(self):
        c = cfg(lane_pacing={"beta": {"reset_anchor": "2026-11-04T12:00", "period_h": 168,
                                      "spend_window_h": 1, "reserve": 1.0}})
        self.assertEqual(LP.choose(ask(asked="beta"), c, live()).lane, "beta")
        self.assertNotEqual(LP.choose(ask(work="writing"), c, live()).lane, "beta")

    def test_explicit_lane_stays_while_its_fallback_is_in_its_spend_window(self):
        # Regression (found by test_runner, 11 Nov 2026 17:19): an explicit codex-like lane held by
        # pacing lost to its claude-like fallback in its use-it-or-lose-it hour, though it could run.
        c = cfg(lane_fallback={"beta": "alpha"},
                lane_pacing={"beta": {"reset_anchor": "2026-11-04T12:00", "period_h": 168,
                                      "spend_window_h": 1, "reserve": 1.0},
                             "alpha": {"reset_anchor": "2026-11-11T12:30", "period_h": 168,
                                       "spend_window_h": 24, "reserve": 0.1}})
        p = LP.choose(ask(asked="beta", work="code-edit"), c, live())
        self.assertEqual((p.lane, p.moved), ("beta", ""))
        self.assertEqual([r["lane"] for r in p.scores], ["beta"], "the hint only counts once beta can't run")
        p = LP.choose(ask(asked="beta"), c, live(limited={"beta": NOW + dt.timedelta(hours=1)}))
        self.assertEqual((p.lane, p.moved), ("alpha", "fallback"))

    def test_fallback_hint_breaks_a_tie(self):
        c = cfg()
        c["lane_strengths"]["gamma"]["code-edit"] = c["lane_strengths"]["beta"]["code-edit"]
        st = live(limited={"alpha": NOW + dt.timedelta(hours=1)}, lanes=["alpha", "gamma", "beta"])
        self.assertEqual(LP.choose(ask(), c, st).lane, "beta", "alpha's lane_fallback hint")

    def test_work_kind(self):
        c = cfg(lane_work_header="Work", lane_work_for_kind={"bugfix": "debug"}, lane_work_default="code-edit")
        self.assertEqual(LP.work_kind({"work": "Writing"}, "x", "work", c), "writing")
        self.assertEqual(LP.work_kind({}, "x", "bugfix", c), "debug")
        self.assertEqual(LP.work_kind({}, "Summarise", "work", c, lambda t: "writing"), "writing")
        self.assertEqual(LP.work_kind({}, "x", "work", c, lambda t: None), "code-edit")


class ConfigTest(unittest.TestCase):
    """Rule 0: the shipped config holds every lane_pick value, with a comment; the code holds none."""
    KEYS = ("lane_auto_word", "lane_strengths", "lane_strength_default", "lane_preference_bonus",
            "bugfix_lane_bonus", "lane_pick_weights", "lane_pick_explicit_scope", "lane_pacing",
            "lane_usage_file", "lane_usage_max_age_h", "reviewer_lanes", "reviewer_work",
            "reviewer_override_lane", "lane_pick_headers", "lane_work_header", "lane_work_for_kind",
            "lane_work_default", "lane_pick_yes_words")

    def test_keys_are_in_config_and_defaults_with_comments(self):
        shipped = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        defaults = json.loads((HERE / "config_defaults.json").read_text(encoding="utf-8"))
        for k in self.KEYS:
            self.assertIn(k, shipped, k)
            self.assertEqual(shipped[k], defaults.get(k), k)
            self.assertIn(f"_{k}", shipped, f"{k} has no _comment")

    def test_shipped_defaults_keep_todays_rules(self):
        c = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        bug = c["bugfix_lane"]
        self.assertGreater(c["bugfix_lane_bonus"], c["lane_preference_bonus"], "bug fixes still go to bugfix_lane")
        held = [l for l, p in c["lane_pacing"].items() if p["reserve"] >= 1]
        self.assertTrue(held and all(c["lane_pacing"][l]["spend_window_h"] > 0 for l in held),
                        "a lane held until the last hours before its reset")
        self.assertLess(c["lane_pacing"][bug]["reserve"], 1)
        for lane in c["reviewer_lanes"]:
            self.assertIn(lane, c["lane_argv"], "reviewers are configured lanes")

    def test_no_lane_names_or_numbers_in_the_code(self):
        c = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        src = (HERE / "lane_pick.py").read_text(encoding="utf-8")
        code = re.sub(r'"""(?:.|\n)*?"""', "", src)  # docstrings may name things
        code = re.sub(r"#.*", "", code)
        for lane in c["lane_argv"]:
            self.assertNotRegex(code, rf"[\"']{lane}[\"']", lane)
        for word in (c["lane_auto_word"], *c["lane_pick_headers"].values()):
            self.assertNotRegex(code, rf"[\"']{re.escape(word)}[\"']", word)


if __name__ == "__main__":
    unittest.main(verbosity=2)
