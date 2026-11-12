"""Regression test for tasks.py -- deadline parsing and task matching.

Uses a temp store so it never touches memory/tasks.json.

A unittest module (it was a print-and-sys.exit script, which made
`unittest discover -p test_tasks.py` error on import); every original check
is kept as it was.
"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tasks
import time_parse

# Wednesday 2026-09-23 21:00, so "friday" is 2 days ahead.
NOW = datetime(2026, 9, 23, 21, 0)


def _fmt(d):
    return d.strftime("%Y-%m-%d %H:%M") if d else None


class DeadlineParsing(unittest.TestCase):
    cases = [
        ("before friday", "2026-09-25 23:59"),
        ("by friday 5pm", "2026-09-25 17:00"),
        ("friday", "2026-09-25 23:59"),
        ("next monday 9am", "2026-09-28 09:00"),
        ("tomorrow 5pm", "2026-09-24 17:00"),
        ("in 2 hours", "2026-09-23 23:00"),
        ("wednesday", "2026-09-30 23:59"),   # today is Wed -> next week, not today
        ("", None),
        ("sometime whenever", None),
        # time_parse, through parse_due (2026-11-03): the old weekday regex
        # read "thursday at 3" as 03:00.
        ("thursday at 3", "2026-09-24 15:00"),
        ("tomorrow morning", "2026-09-24 09:00"),
        ("at half past four", "2026-09-24 16:30"),  # 16:30 today has gone
        ("on the 12th", "2026-10-12 23:59"),
        ("in 20 mins", "2026-09-23 21:20"),
    ]

    def test_parse_due(self):
        for text, want in self.cases:
            with self.subTest(text=text):
                self.assertEqual(_fmt(tasks.parse_due(text, now=NOW)), want)

    def test_naive_store_times(self):
        self.assertIsNone(tasks.parse_due("tomorrow 5pm", now=NOW).tzinfo)

    def test_default_clock_is_time_parse_clock(self):
        with time_parse.fixed_clock(NOW):
            self.assertEqual(_fmt(tasks.parse_due("friday")), "2026-09-25 23:59")


class DeadlineWordsFromData(unittest.TestCase):
    """Rule 0: tasks.time_words and tasks.clock_lead_words in reminders.json
    decide whether a dateparser deadline keeps its clock time."""

    def setUp(self):
        import json
        import os
        import reminders_config

        self._cfg, self._env = reminders_config, os.environ.get("DENISSE_REMINDERS")
        data = json.loads(Path(reminders_config.DEFAULT_FILE).read_text(encoding="utf-8"))
        data["tasks"]["time_words"].append("week")
        data["tasks"]["clock_lead_words"].append("near")
        path = Path(tempfile.mkdtemp()) / "reminders.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        os.environ["DENISSE_REMINDERS"] = str(path)
        reminders_config.reload()

    def tearDown(self):
        import os

        if self._env is None:
            os.environ.pop("DENISSE_REMINDERS", None)
        else:
            os.environ["DENISSE_REMINDERS"] = self._env
        self._cfg.reload()

    def test_a_time_word_keeps_the_clock(self):
        # Default data: "next week" (dateparser) is the end of that day.
        # With "week" listed as a time word, dateparser's clock is kept.
        self.assertEqual(_fmt(tasks.parse_due("next week", now=NOW)), "2026-09-30 21:00")

    def test_clock_lead_words(self):
        self.assertTrue(tasks._has_time().search("near 5"))
        self.tearDown()
        self.assertFalse(tasks._has_time().search("near 5"))
        self.assertEqual(_fmt(tasks.parse_due("next week", now=NOW)), "2026-09-30 23:59")


class DataChangesTaskParsing(unittest.TestCase):
    """Rule 0, third pass (12 Nov): dateparser's settings come from
    reminders.json tasks.dateparser_settings; weekday and month names and
    the clock separators from time_parse.json -- not the calendar module."""

    def _with_reminders(self, change):
        import json
        import os
        import reminders_config

        data = json.loads(Path(reminders_config.DEFAULT_FILE).read_text(encoding="utf-8"))
        change(data)
        path = Path(tempfile.mkdtemp()) / "reminders.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        before = os.environ.get("DENISSE_REMINDERS")
        os.environ["DENISSE_REMINDERS"] = str(path)
        reminders_config.reload()

        def undo():
            if before is None:
                os.environ.pop("DENISSE_REMINDERS", None)
            else:
                os.environ["DENISSE_REMINDERS"] = before
            reminders_config.reload()
        self.addCleanup(undo)

    def test_dateparser_settings(self):
        # "march" is read by dateparser alone; the shipped data prefers the future.
        self.assertEqual(_fmt(tasks.parse_due("march", now=NOW)), "2027-03-23 23:59")
        self._with_reminders(lambda d: d["tasks"]["dateparser_settings"].update(
            PREFER_DATES_FROM="past"))
        self.assertEqual(_fmt(tasks.parse_due("march", now=NOW)), "2026-03-23 23:59")

    def test_weekday_names_are_time_parse_data(self):
        from unittest import mock

        names = ["monz", "tuez", "wedz", "thurz", "friz", "satz", "sunz"]
        # time_parse compiled its own patterns at import, so only the tasks
        # weekday rule sees the new names: 'friz' is the end of the coming Friday.
        self.assertNotEqual(_fmt(tasks.parse_due("friz", now=NOW)), "2026-09-25 23:59")
        with mock.patch.object(time_parse, "WEEKDAYS", names):
            self.assertEqual(_fmt(tasks.parse_due("friz", now=NOW)), "2026-09-25 23:59")
        self.assertEqual(tasks._weekdays(), time_parse.CONFIG["weekdays"])

    def test_month_and_clock_words_are_time_parse_data(self):
        n = time_parse.CONFIG["month_abbreviation_length"]
        self.assertEqual(tasks._month_words()[:12], [m[:n] for m in time_parse.CONFIG["months"]])
        self.assertTrue(tasks._has_time().search("15.30"))
        from unittest import mock

        with mock.patch.object(time_parse, "_CLOCK_SEP", "[:]"):
            self.assertFalse(tasks._has_time().search("15.30"))


class TaskStore(unittest.TestCase):
    def setUp(self):
        self._file, self._mirror = tasks.TASKS_FILE, tasks._mirror_to_vault
        tasks.TASKS_FILE = str(Path(tempfile.mkdtemp()) / "tasks.json")
        tasks._mirror_to_vault = lambda: None

    def tearDown(self):
        tasks.TASKS_FILE, tasks._mirror_to_vault = self._file, self._mirror

    def test_good_file_round_trips_unchanged(self):
        import json
        stored = [{"id": "abc12345", "text": "café run", "due": None, "due_text": None,
                   "created": "2026-11-10T09:00", "done_at": None, "due_inferred": False,
                   "event_id": None, "reminded_on": None, "priority": "normal"}]
        with open(tasks.TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(stored, f, indent=2, ensure_ascii=False)
        with open(tasks.TASKS_FILE, "rb") as f:
            before = f.read()
        tasks._save(tasks._load())
        with open(tasks.TASKS_FILE, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_torn_file_is_kept_aside_not_erased(self):
        import logging
        import safe_json
        logging.getLogger("denisse.safe_json").setLevel(logging.ERROR)
        torn = '[{"id": "abc12345", "text": "email Sunway", "done_'
        with open(tasks.TASKS_FILE, "w", encoding="utf-8") as f:
            f.write(torn)
        self.assertEqual(tasks.list_tasks(), [])
        tasks.add_task("buy a new soldering tip")  # the tray keeps working
        self.assertEqual([t["text"] for t in tasks.list_tasks()], ["buy a new soldering tip"])
        copies = safe_json.corrupt_copies(tasks.TASKS_FILE)
        self.assertEqual(len(copies), 1)
        with open(copies[0], encoding="utf-8") as f:
            self.assertEqual(f.read(), torn)

    def test_store_and_matching(self):
        tasks.add_task("email Sunway about the firm visit", "before friday")
        tasks.add_task("submit the EE problem set", "tomorrow 5pm")
        tasks.add_task("buy a new soldering tip")

        open_now = tasks.list_tasks()
        self.assertEqual(len(open_now), 3, "three open tasks")
        self.assertTrue(open_now[0]["text"].startswith("submit"), "soonest deadline first")
        self.assertTrue(open_now[-1]["text"].startswith("buy"), "undated sorts last")
        self.assertEqual(open_now[0]["due_text"], "tomorrow 5pm", "raw phrase kept")

        # Fuzzy match on words, not the id.
        done = tasks.complete_task("the sunway email")
        self.assertTrue(done is not None and "Sunway" in done["text"],
                        "fuzzy match completes the right task")
        self.assertEqual(len(tasks.list_tasks()), 2, "done task hidden")
        self.assertEqual(len(tasks.list_tasks(include_done=True)), 3, "done task kept")
        self.assertIsNone(tasks.complete_task("the sunway email"),
                          "completing it again finds nothing")

        # Ambiguity must refuse rather than guess.
        tasks.add_task("call mum about the flight")
        tasks.add_task("book the flight")
        self.assertIsNone(tasks.complete_task("flight"), "ambiguous match refuses")
        self.assertIsNotNone(tasks.complete_task("book flight"), "specific match still works")

    def test_add_task_tool_echoes_and_asks(self):
        import tools

        with time_parse.fixed_clock(NOW):  # a Wednesday evening
            said = tools.add_task("call the dentist", "thursday at 3")
            self.assertEqual(said, "Added: call the dentist -- due Thursday 24 Sep, 15:00.")
            asked = tools.add_task("water the plants", "wednesday")
            self.assertTrue(asked.startswith("Not added yet"), asked)
            self.assertIn("next Wednesday", asked)
            unread = tools.add_task("tidy the desk", "whenever")
            self.assertIn("couldn't work out a date", unread)
        texts = [t["text"] for t in tasks.list_tasks()]
        self.assertIn("call the dentist", texts)
        self.assertNotIn("water the plants", texts)  # asked, so nothing was added


if __name__ == "__main__":
    unittest.main(verbosity=2)
