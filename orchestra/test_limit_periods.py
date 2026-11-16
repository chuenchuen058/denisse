"""Allowances that reset on a calendar boundary (runner.period_until, config limit_periods)."""
import datetime as dt
import unittest

import runner

UTC = dt.timezone.utc


def local(*a):
    return dt.datetime(*a, tzinfo=UTC).astimezone().replace(tzinfo=None)


class PeriodUntilTest(unittest.TestCase):
    def setUp(self):
        self.saved = runner.C.get("limit_periods")
        runner.C["limit_periods"] = [
            {"pattern": "monthly quota", "every": "month", "at": "00:00", "utc_offset_h": 0},
            {"pattern": "daily limit", "every": "day", "at": "00:00", "utc_offset_h": 0},
            {"pattern": "weekly cap", "every": "week", "weekday": 0, "at": "09:00", "utc_offset_h": 0}]

    def tearDown(self):
        runner.C["limit_periods"] = self.saved

    def test_copilot_monthly_quota_waits_for_the_first(self):
        msg = "You have exceeded your monthly quota (Request ID: x)"
        at = local(2026, 11, 14, 6, 24)
        self.assertEqual(runner.period_until(msg, at), local(2026, 12, 1, 0, 0))

    def test_december_rolls_into_january(self):
        self.assertEqual(runner.period_until("monthly quota", local(2026, 12, 20, 12, 0)), local(2027, 1, 1, 0, 0))

    def test_daily_and_weekly(self):
        self.assertEqual(runner.period_until("daily limit", local(2026, 11, 16, 23, 0)), local(2026, 11, 17, 0, 0))
        # 16 Nov 2026 is a Monday: after 09:00 the next reset is the following Monday
        self.assertEqual(runner.period_until("weekly cap", local(2026, 11, 16, 10, 0)), local(2026, 11, 23, 9, 0))

    def test_no_rule_no_time(self):
        self.assertIsNone(runner.period_until("something else broke", local(2026, 11, 16, 10, 0)))

    def test_message_with_a_time_still_wins(self):
        hit = "try again at Nov 18th, 2026 4:58 PM. monthly quota"
        self.assertEqual(runner.parse_limit_until(hit, dt.datetime(2026, 11, 16, 10, 0)), dt.datetime(2026, 11, 18, 16, 58))


if __name__ == "__main__":
    unittest.main()
