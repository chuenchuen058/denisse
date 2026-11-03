"""reminders.json drives tasks.py, timers.py, timer_ui.py and reminder_card.py.

Rule 0 (AGENTS.md): the wording, durations, colours, limits and word lists
those modules used to hold inline are data now. These tests pin two things:
the shipped data validates cleanly (so behaviour with it is what the other
test modules already check), and changing the data changes the behaviour --
at least once per module -- while a bad value logs and falls back to the
shipped default instead of crashing.

No Tk window is opened: only the card's pure helpers are called.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import reminders_config  # noqa: E402

DEFAULTS = json.loads((ROOT / "reminders.defaults.json").read_text(encoding="utf-8"))


class _Data(unittest.TestCase):
    """Each test writes its own reminders.json and points the loader at it."""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("DENISSE_REMINDERS", "DENISSE_USER_NAME")}
        os.environ["DENISSE_USER_NAME"] = "Ada Lovelace"
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reminders_config.reload()

    def use(self, edit=None, raw=None):
        data = copy.deepcopy(DEFAULTS)
        if edit:
            edit(data)
        p = self.tmp / "reminders.json"
        p.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")
        os.environ["DENISSE_REMINDERS"] = str(p)
        with self.assertLogs("denisse.reminders", "WARNING") if raw is not None else self.assertNoLogs("denisse.reminders", "WARNING"):
            reminders_config.reload()


class Loader(_Data):
    def test_shipped_files_validate_cleanly(self):
        for name in ("reminders.json", "reminders.defaults.json"):
            with self.subTest(name=name):
                os.environ["DENISSE_REMINDERS"] = str(ROOT / name)
                with self.assertNoLogs("denisse.reminders", "WARNING"):
                    reminders_config.reload()

    def test_bad_values_fall_back_with_a_log_line(self):
        def edit(d):
            d["timers"]["max_minutes"] = "lots"
            d["card"]["colours"]["warn"] = "amber"
            d["timers"]["messages"]["started"] = "{what} for {nonsense}"
            d["card"]["layout"]["list_max_frac"] = 3
            d["priorities"][0]["colour"] = "red"
            d["tasks"]["default_priority"] = "urgent"
            del d["card"]["text"]["dismiss"]
        data = copy.deepcopy(DEFAULTS)
        edit(data)
        p = self.tmp / "reminders.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        os.environ["DENISSE_REMINDERS"] = str(p)
        with self.assertLogs("denisse.reminders", "WARNING") as cm:
            reminders_config.reload()
        said = "\n".join(cm.output)
        for key in ("timers.max_minutes", "card.colours.warn", "timers.messages.started",
                    "card.layout.list_max_frac", "priorities[0].colour", "tasks.default_priority", "card.text.dismiss"):
            self.assertIn(key, said)
        self.assertEqual(reminders_config.get("timers.max_minutes"), DEFAULTS["timers"]["max_minutes"])
        self.assertEqual(reminders_config.get("card.colours.warn"), DEFAULTS["card"]["colours"]["warn"])
        self.assertEqual(reminders_config.get("tasks.default_priority"), "medium")
        self.assertEqual(reminders_config.get("card.text.dismiss"), "Dismiss")

    def test_unreadable_file_uses_the_shipped_defaults(self):
        self.use(raw="{ not json")
        self.assertEqual(reminders_config.get("timers.max_minutes"), DEFAULTS["timers"]["max_minutes"])

    def test_user_is_the_given_name(self):
        self.use()
        self.assertEqual(reminders_config.user(), "Ada")


class Tasks(_Data):
    def setUp(self):
        super().setUp()
        import tasks
        self.tasks = tasks
        self._file, self._mirror = tasks.TASKS_FILE, tasks._mirror_to_vault
        tasks.TASKS_FILE = str(self.tmp / "tasks.json")
        tasks._mirror_to_vault = lambda: None

    def tearDown(self):
        self.tasks.TASKS_FILE, self.tasks._mirror_to_vault = self._file, self._mirror
        super().tearDown()

    def test_ignore_words_drive_matching(self):
        self.use()
        self.tasks.add_task("submit the EE problem set")
        self.assertIsNone(self.tasks.complete_task("the thing"), "'the' carries no weight as shipped")
        self.use(lambda d: d["tasks"]["match"].update(ignore_words=[]))
        self.assertIsNotNone(self.tasks.complete_task("the thing"), "with no ignore words 'the' matches")

    def test_describe_wording(self):
        self.use()
        self.assertEqual(self.tasks.describe({"text": "tidy", "due": None, "due_text": "asap"}), "tidy (asap)")
        self.use(lambda d: d["tasks"]["describe"].update(undated_note="{text} [{note}]"))
        self.assertEqual(self.tasks.describe({"text": "tidy", "due": None, "due_text": "asap"}), "tidy [asap]")

    def test_priorities_and_id_length(self):
        def edit(d):
            d["priorities"] = [{"name": "now", "label": "Now", "colour": "#ff0000"},
                               {"name": "later", "label": "Later", "colour": "#00ff00"}]
            d["tasks"].update(default_priority="later", id_length=6)
        self.use(edit)
        t = self.tasks.add_task("water the plants", priority="medium")
        self.assertEqual(t["priority"], "later")
        self.assertEqual(len(t["id"]), 6)
        self.assertEqual(self.tasks.add_task("ring the bank", priority="NOW")["priority"], "now")


class Timers(_Data):
    def setUp(self):
        super().setUp()
        import timers
        self.timers = timers
        timers._timers.clear()

    def tearDown(self):
        self.timers._timers.clear()
        super().tearDown()

    def test_wording_limit_and_labels_come_from_data(self):
        self.use()
        self.assertIn("Ask Ada how long", self.timers.set_timer())
        def edit(d):
            d["timers"]["max_minutes"] = 5
            d["timers"]["filler_labels"] = ["oven"]
            d["timers"]["messages"]["unnamed"] = "Countdown"
            d["timers"]["messages"]["too_long"] = "Too long, {user}."
            d["timers"]["units"]["minute"] = ["min", "mins"]
        self.use(edit)
        self.assertEqual(self.timers.set_timer(minutes=10), "Too long, Ada.")
        said = self.timers.set_timer(minutes=2, label="oven")
        self.assertTrue(said.startswith("Countdown started: 2 mins"), said)


class TimerUI(_Data):
    def test_finished_message_comes_from_data(self):
        import timer_ui
        self.use()
        self.assertEqual(timer_ui.finished_lines({"label": ""})[0], "Time's up, Ada.")
        self.use(lambda d: d["timer_ui"].update(done_named="Ding -- {what}.", notify_app="Orb"))
        line, what, app = timer_ui.finished_lines({"label": "oven"})
        self.assertEqual((line, what, app), ("Ding -- oven timer.", "oven timer", "Orb"))


class ReminderCardText(_Data):
    def setUp(self):
        super().setUp()
        import reminder_card
        self.card = reminder_card

    def test_greeting_bands(self):
        self.use()
        self.assertEqual(self.card._greeting(datetime(2026, 1, 1, 11)), "Morning")
        self.assertEqual(self.card._greeting(datetime(2026, 1, 1, 20)), "Evening")
        self.use(lambda d: d["card"].update(greetings=[{"until_hour": 10, "word": "Hiya"},
                                                       {"until_hour": 24, "word": "Hello"}]))
        self.assertEqual(self.card._greeting(datetime(2026, 1, 1, 11)), "Hello")

    def test_summary_due_line_and_reading_time(self):
        self.use()
        two = [{"priority": "high"}, {"priority": None}]
        self.assertEqual(self.card._summary(two), "You've got 2 things on your plate (1 high, 1 medium):")
        def edit(d):
            d["card"]["text"].update(summary_many="{n} to do: {counts}", count_join=" / ",
                                     no_deadline="whenever", tomorrow="Tmrw{clock}")
            d["card"]["timing"].update(reply_min_ms=1000, reply_base_ms=1)
        self.use(edit)
        self.assertEqual(self.card._summary(two), "2 to do: 1 high / 1 medium")
        self.assertEqual(self.card._due_line({"due": None})[0], "whenever")
        tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0)
        self.assertEqual(self.card._due_line({"due": tomorrow.isoformat()})[0], "Tmrw 09:00")
        self.assertEqual(self.card._reading_ms("hi"), 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
