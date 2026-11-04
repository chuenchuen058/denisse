"""time_parse.py: spoken time phrases -> Europe/London datetimes.

Every case runs against a pinned clock (`at(...)` / time_parse.fixed_clock),
never the real one, so a pass on a Tuesday means the same on a Sunday.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time_parse as tp

# Tuesday 3 Nov 2026, 10:00 GMT.
TUE = datetime(2026, 11, 3, 10, 0)


def at(*args):
    """The fixed-clock helper: a London wall-clock moment."""
    return datetime(*args, tzinfo=tp.LONDON)


def resolved(phrase, now=TUE):
    r = tp.parse(phrase, now)
    if r.when is None:
        raise AssertionError(f"{phrase!r} did not resolve: question={r.question!r} left={r.leftover!r}")
    return r.when.strftime("%Y-%m-%d %H:%M")


class Weekdays(unittest.TestCase):
    def test_weekday_with_bare_hour_is_afternoon(self):
        self.assertEqual(resolved("Thursday at 3"), "2026-11-05 15:00")
        self.assertEqual(resolved("remind me Thursday at 3"), "2026-11-05 15:00")

    def test_this_and_next(self):
        self.assertEqual(resolved("this friday"), "2026-11-06 23:59")
        self.assertEqual(resolved("next friday 5pm"), "2026-11-06 17:00")  # the first Friday after today
        self.assertEqual(resolved("this tuesday"), "2026-11-03 23:59")     # today, still to come
        self.assertEqual(resolved("next tuesday"), "2026-11-10 23:59")

    def test_next_monday_before_the_lecture(self):
        r = tp.parse("next Monday before the lecture", TUE)
        self.assertEqual(r.when.strftime("%Y-%m-%d %H:%M"), "2026-11-09 23:59")
        self.assertFalse(r.has_time)
        self.assertEqual(r.leftover, "lecture")  # kept visible, not silently dropped

    def test_today_named_is_ambiguous(self):
        # "Thursday at 3" said on Thursday morning: today or next week? Ask.
        r = tp.parse("Thursday at 3", at(2026, 11, 5, 9, 0))
        self.assertIsNone(r.when)
        self.assertIn("today", r.question)
        self.assertIn("Thursday 12 Nov", r.question)
        r = tp.parse("tuesday", TUE)
        self.assertIsNone(r.when)
        self.assertTrue(r.question)

    def test_today_named_after_that_time_is_next_week(self):
        self.assertEqual(resolved("tuesday at 9"), "2026-11-10 09:00")

    def test_abbreviations(self):
        self.assertEqual(resolved("thurs 3pm"), "2026-11-05 15:00")
        self.assertEqual(resolved("fri"), "2026-11-06 23:59")


class RelativeSpans(unittest.TestCase):
    def test_minutes_hours_days(self):
        self.assertEqual(resolved("in 20 mins"), "2026-11-03 10:20")
        self.assertEqual(resolved("in an hour"), "2026-11-03 11:00")
        self.assertEqual(resolved("in an hour and a half"), "2026-11-03 11:30")
        self.assertEqual(resolved("in half an hour"), "2026-11-03 10:30")
        self.assertEqual(resolved("in 1h30"), "2026-11-03 11:30")
        self.assertEqual(resolved("in 2 hours 15 minutes"), "2026-11-03 12:15")
        self.assertEqual(resolved("20 minutes from now"), "2026-11-03 10:20")
        self.assertEqual(resolved("in 2 days"), "2026-11-05 10:00")
        self.assertEqual(resolved("in a week"), "2026-11-10 10:00")

    def test_span_across_the_clock_change_is_elapsed_time(self):
        # Clocks go back at 02:00 BST on 25 Oct 2026. 01:45 BST + 30 min
        # is 01:15 GMT, not 02:15.
        r = tp.parse("in 30 minutes", at(2026, 10, 25, 1, 45))
        utc = lambda d: d.astimezone(timezone.utc)  # same-zone subtraction is wall-clock
        self.assertEqual(utc(r.when) - utc(at(2026, 10, 25, 1, 45)), timedelta(minutes=30))
        self.assertEqual(r.when.utcoffset(), timedelta(0))
        self.assertEqual(r.when.strftime("%H:%M"), "01:15")

    def test_parse_duration(self):
        self.assertEqual(tp.parse_duration("20 mins"), timedelta(minutes=20))
        self.assertEqual(tp.parse_duration("90 seconds"), timedelta(seconds=90))
        self.assertEqual(tp.parse_duration("a quarter of an hour"), timedelta(minutes=15))
        self.assertEqual(tp.parse_duration("1h 30m"), timedelta(minutes=90))
        self.assertIsNone(tp.parse_duration("20 mins of something"))
        self.assertIsNone(tp.parse_duration(""))
        self.assertIsNone(tp.parse_duration("soon"))


class PartsOfDay(unittest.TestCase):
    def test_documented_defaults(self):
        self.assertEqual(resolved("tomorrow morning"), "2026-11-04 09:00")
        self.assertEqual(resolved("tomorrow lunchtime"), "2026-11-04 12:00")
        self.assertEqual(resolved("tomorrow afternoon"), "2026-11-04 14:00")
        self.assertEqual(resolved("tomorrow evening"), "2026-11-04 18:00")
        self.assertEqual(resolved("tonight"), "2026-11-03 20:00")
        self.assertEqual(resolved("tmrw"), "2026-11-04 23:59")

    def test_part_sets_am_pm(self):
        self.assertEqual(resolved("tomorrow evening at 7"), "2026-11-04 19:00")
        self.assertEqual(resolved("tomorrow morning at 7"), "2026-11-04 07:00")
        self.assertEqual(resolved("tomorrow afternoon at 2:30"), "2026-11-04 14:30")

    def test_contradiction_asks(self):
        r = tp.parse("tomorrow morning at 3pm", TUE)
        self.assertIsNone(r.when)
        self.assertTrue(r.question)

    def test_tomorrow_after_midnight_asks(self):
        r = tp.parse("tomorrow morning", at(2026, 11, 4, 0, 30))
        self.assertIsNone(r.when)
        self.assertIn("midnight", r.question)
        self.assertEqual(resolved("tomorrow morning", at(2026, 11, 4, 6, 0)), "2026-11-05 09:00")


class ClockTimes(unittest.TestCase):
    def test_half_past_quarter_to(self):
        self.assertEqual(resolved("at half past four"), "2026-11-03 16:30")
        self.assertEqual(resolved("half four"), "2026-11-03 16:30")   # British
        self.assertEqual(resolved("quarter past 11"), "2026-11-03 11:15")
        self.assertEqual(resolved("quarter to 5"), "2026-11-03 16:45")
        self.assertEqual(resolved("twenty to six tomorrow"), "2026-11-04 17:40")
        self.assertEqual(resolved("5 past 4"), "2026-11-03 16:05")

    def test_12h_and_24h(self):
        self.assertEqual(resolved("3:30pm"), "2026-11-03 15:30")
        self.assertEqual(resolved("tomorrow 9am"), "2026-11-04 09:00")
        self.assertEqual(resolved("tomorrow 12am"), "2026-11-04 00:00")
        self.assertEqual(resolved("tomorrow 12pm"), "2026-11-04 12:00")
        self.assertEqual(resolved("15:00"), "2026-11-03 15:00")
        self.assertEqual(resolved("tomorrow 09:00"), "2026-11-04 09:00")
        self.assertEqual(resolved("tomorrow 15.30"), "2026-11-04 15:30")
        self.assertEqual(resolved("tomorrow at 17"), "2026-11-04 17:00")
        self.assertEqual(resolved("noon"), "2026-11-03 12:00")
        self.assertEqual(resolved("friday midday"), "2026-11-06 12:00")
        self.assertEqual(resolved("4 o'clock"), "2026-11-03 16:00")

    def test_bare_hour_with_no_day_is_the_next_one(self):
        self.assertEqual(resolved("at 8", at(2026, 11, 3, 18, 0)), "2026-11-03 20:00")
        self.assertEqual(resolved("at 8", at(2026, 11, 3, 21, 0)), "2026-11-04 08:00")
        self.assertEqual(resolved("at 3", at(2026, 11, 3, 16, 0)), "2026-11-04 15:00")

    def test_time_today_already_passed_asks(self):
        r = tp.parse("today at 9", TUE)
        self.assertIsNone(r.when)
        self.assertIn("passed", r.question)


class Dates(unittest.TestCase):
    def test_ordinals(self):
        self.assertEqual(resolved("on the 12th"), "2026-11-12 23:59")
        self.assertEqual(resolved("the 2nd"), "2026-12-02 23:59")        # passed this month
        self.assertEqual(resolved("on the 31st"), "2026-12-31 23:59")    # November has no 31st
        self.assertEqual(resolved("on the 3rd at 9"), "2026-12-03 09:00")  # 09:00 today has gone
        r = tp.parse("on the 3rd", TUE)  # today is the 3rd and the day isn't over
        self.assertIsNone(r.when)
        self.assertTrue(r.question)

    def test_months(self):
        self.assertEqual(resolved("12 nov"), "2026-11-12 23:59")
        self.assertEqual(resolved("nov 12th 14:00"), "2026-11-12 14:00")
        self.assertEqual(resolved("the 6th of October 14:00"), "2027-10-06 14:00")  # gone this year
        self.assertEqual(resolved("12th december 2026 at 10am"), "2026-12-12 10:00")
        self.assertEqual(resolved("2026-11-20"), "2026-11-20 23:59")

    def test_uk_numeric_order(self):
        self.assertEqual(resolved("12/11"), "2026-11-12 23:59")
        self.assertEqual(resolved("1/12/2026 3pm"), "2026-12-01 15:00")

    def test_weekday_must_match_date(self):
        self.assertEqual(resolved("friday the 13th"), "2026-11-13 23:59")
        r = tp.parse("friday the 12th", TUE)
        self.assertIsNone(r.when)
        self.assertIn("Thursday", r.question)

    def test_past_year_asks(self):
        r = tp.parse("12th december 2025", TUE)
        self.assertIsNone(r.when)
        self.assertIn("passed", r.question)

    def test_before(self):
        # The two conventions tasks.py already had (and test_tasks.py checks).
        self.assertEqual(resolved("before friday"), "2026-11-06 23:59")
        self.assertEqual(resolved("before 12 nov"), "2026-11-11 23:59")
        self.assertEqual(resolved("before 12 nov 3pm"), "2026-11-12 15:00")


class NotUnderstood(unittest.TestCase):
    def test_returns_nothing_rather_than_half_a_guess(self):
        for phrase in ("", "   ", "sometime whenever", "next week", "3 days before friday",
                       "end of the month", "asap"):
            r = tp.parse(phrase, TUE)
            self.assertIsNone(r.when, phrase)
            self.assertIsNone(r.question, phrase)

    def test_impossible_clock(self):
        self.assertIsNone(tp.parse("tomorrow 25:00", TUE).when)
        self.assertIsNone(tp.parse("on the 32nd", TUE).when)


class EchoAndClock(unittest.TestCase):
    def test_describe(self):
        self.assertEqual(tp.describe(at(2026, 11, 5, 15, 0), TUE), "Thursday 5 Nov, 15:00")
        self.assertEqual(tp.describe(at(2027, 1, 4, 9, 0), TUE), "Monday 4 Jan 2027, 09:00")

    def test_fixed_clock(self):
        with tp.fixed_clock(datetime(2026, 11, 5, 9, 0)) as pinned:
            self.assertEqual(tp.now(), pinned)
            self.assertEqual(tp.now().tzinfo, tp.LONDON)
            self.assertTrue(tp.parse("Thursday at 3").question)  # uses the pinned clock
            self.assertEqual(tp.parse("tomorrow 9am").when, at(2026, 11, 6, 9, 0))
        self.assertIsNone(tp._fixed)  # released on exit

    def test_naive_and_aware_now_agree(self):
        self.assertEqual(tp.parse("in 20 mins", TUE).when,
                         tp.parse("in 20 mins", at(2026, 11, 3, 10, 0)).when)


class TimerDuration(unittest.TestCase):
    """timers.set_timer(duration=...) -- his words, parsed here, not by the model."""

    def tearDown(self):
        import timers
        timers.cancel_timer("all")

    def test_words_become_the_length(self):
        import timers
        said = timers.set_timer(duration="an hour and a half", label="oven")
        self.assertTrue(said.startswith("'oven' timer started: 1 hour 30 minutes"), said)
        self.assertEqual(timers.active()[0]["minutes"], 90)

    def test_numbers_win_over_words(self):
        import timers
        timers.set_timer(minutes=5, duration="20 mins")
        self.assertEqual(timers.active()[0]["minutes"], 5)

    def test_unreadable_length_starts_nothing(self):
        import timers
        said = timers.set_timer(duration="a while")
        self.assertTrue(said.startswith("No timer started"), said)
        self.assertEqual(timers.active(), [])


class ConfigTest(unittest.TestCase):
    """Rule 0: the words and defaults come from time_parse.json."""

    def test_defaults_come_from_the_json(self):
        cfg = tp.CONFIG
        self.assertEqual(str(tp.TZ), cfg["timezone"])
        for part, hhmm in cfg["day_parts"].items():
            self.assertEqual(tp.part_time(part).strftime("%H:%M"), hhmm)
        self.assertEqual(tp.WEEKDAYS, cfg["weekdays"])
        self.assertEqual(tp.MONTHS, cfg["months"])


class PartSourceTest(unittest.TestCase):
    """The hook the learner model will use to say what 'morning' means for
    Chuen. The learner isn't wired; a fake belief stands in for it."""

    def tearDown(self):
        tp.set_part_source(None)

    @staticmethod
    def _from_beliefs(beliefs):
        # Stand-in for whatever turns learner beliefs into day-part times.
        def source():
            return {b["part"]: b["time"] for b in beliefs if b.get("status") == "current"}
        return source

    def test_a_belief_moves_morning(self):
        belief = {"claim": "Chuen is never up before 10", "part": "morning",
                  "time": "10:30", "status": "current"}
        tp.set_part_source(self._from_beliefs([belief]))
        self.assertEqual(resolved("tomorrow morning"), "2026-11-04 10:30")
        # Parts the belief says nothing about keep the JSON default.
        self.assertEqual(resolved("tomorrow evening"),
                         "2026-11-04 " + tp.CONFIG["day_parts"]["evening"])

    def test_removing_the_source_restores_the_default(self):
        tp.set_part_source(lambda: {"morning": "10:30"})
        tp.set_part_source(None)
        self.assertEqual(resolved("tomorrow morning"),
                         "2026-11-04 " + tp.CONFIG["day_parts"]["morning"])

    def test_a_broken_source_falls_back(self):
        def boom():
            raise RuntimeError("learner store unreadable")
        default = "2026-11-04 " + tp.CONFIG["day_parts"]["morning"]
        tp.set_part_source(boom)
        self.assertEqual(resolved("tomorrow morning"), default)
        tp.set_part_source(lambda: {"morning": "25:99"})
        self.assertEqual(resolved("tomorrow morning"), default)
        tp.set_part_source(lambda: {"morning": 7})
        self.assertEqual(resolved("tomorrow morning"), default)


class OnlyATime(unittest.TestCase):
    """A reminder whose text is nothing but its time has no subject
    (tool_policy's task_has_subject check, 2026-11-04)."""

    def test_a_bare_time_is_only_a_time(self):
        for text in ("Thursday", "remind me thursday", "tomorrow at 5", "at 5", "in 20 mins", "tonight", "next monday 9am"):
            self.assertTrue(tp.only_a_time(text), text)

    def test_anything_with_a_subject_is_not(self):
        for text in ("robotics meeting", "call mum", "dentist tomorrow", "lunch", "", "email Ben friday at 5"):
            self.assertFalse(tp.only_a_time(text), text)


if __name__ == "__main__":
    unittest.main()
