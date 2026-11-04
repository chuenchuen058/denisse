"""tray.json drives the tray's commands, routes and wording: the shipped
data reproduces the old behaviour, and changing the data changes it.
No Tk window is opened, no model is called."""

import copy
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import tray  # noqa: E402


def _strip(d):
    return {k: _strip(v) for k, v in d.items() if not k.startswith("_")} if isinstance(d, dict) else d


def _write_config(test, edit):
    with open(tray.DEFAULTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    edit(data)
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    test.addCleanup(os.remove, path)
    return path


def _popup(text, brain=None, busy=False):
    """A ChatPopup with no window: just what _send touches."""
    popup = tray.ChatPopup.__new__(tray.ChatPopup)
    popup.entry = Mock()
    popup.entry.get.return_value = text
    popup.busy, popup.brain = busy, brain
    popup.close = Mock()
    popup._add_system = Mock()
    popup._add_bubble = Mock()
    popup._set_send_enabled = Mock()
    popup.typing_label = Mock()
    return popup


class Base(unittest.TestCase):
    def setUp(self):
        tray.configure(tray.CONFIG_FILE)

    def tearDown(self):
        tray.configure(tray.CONFIG_FILE)
        tray._manual_offline.clear()


class ShippedData(Base):
    def test_shipped_file_equals_defaults(self):
        with open(tray.CONFIG_FILE, encoding="utf-8") as a, open(tray.DEFAULTS_FILE, encoding="utf-8") as b:
            self.assertEqual(_strip(json.load(a)), _strip(json.load(b)))

    def test_defaults_reproduce_the_old_constants(self):
        self.assertEqual(tray.DASHBOARD_URL, "http://127.0.0.1:%d" % tray.mission_control.PORT)
        self.assertEqual(tray.ACCENT, (168, 85, 247))
        self.assertEqual(tray.BG, (10, 7, 16))
        self.assertEqual(tray.BUBBLE_OUT_FILL, (168, 85, 247, 40))
        self.assertEqual(tray.BUBBLE_IN_FILL, (58, 42, 92, 26))
        self.assertEqual((tray.CHAT_WIDTH, tray.CHAT_HEIGHT), (320, 380))
        self.assertEqual(tray.THINKING_MAX_MS, 120_000)
        self.assertEqual(tray.NEWS_POLL_SECONDS, 30 * 60)
        self.assertEqual(tray.POPUP_BG, "#050308")
        self.assertEqual(tray.ACCENT_BRIGHT, "#c9a3ff")

    def test_old_phrases_still_match(self):
        self.assertEqual(tray._match_mode_command("Offline mode."), "offline")
        self.assertEqual(tray._match_mode_command("  online MODE! "), "online")
        self.assertIsNone(tray._match_mode_command("go offline mode please"))
        for text in ("reminders", "show my reminders", "open the task list", "Pull up my to-do list please."):
            self.assertTrue(tray._is_reminder_command(text), text)
        self.assertFalse(tray._is_reminder_command("remind me to call the dentist"))

    def test_menu_and_study_wording(self):
        labels = [i.text for i in tray._build_menu().items if i.text]
        self.assertEqual(labels[0], "Talk to Denisse")
        self.assertIn('Listen for "hey Denisse"', labels)
        self.assertEqual(labels[-1], "Quit")
        self.assertEqual([i.text for i in tray._build_menu("Ctrl+Alt+D").items][0],
                         "Talk to Denisse (Ctrl+Alt+D)")
        with patch.object(tray, "_study_mode", None):
            self.assertEqual(tray._study_menu_text(None), "Study mode (25 / 5)")

    def test_default_routes_are_the_tray(self):
        # Shipped behaviour: matched study/reminder commands never reach the model.
        brain = Mock()
        with patch.object(tray, "_start_study_mode") as start, \
                patch.object(tray, "_match_study_command", return_value="study"):
            self.assertEqual(_popup("study mode", brain=brain)._send(), "break")
        start.assert_called_once()
        brain.respond.assert_not_called()


class DataChangesBehaviour(Base):
    def test_mode_phrases_from_the_data(self):
        def edit(d):
            d["commands"]["offline_phrases"] = ["go dark"]
            d["commands"]["online_phrases"] = ["wake up", "back on"]
        tray.configure(_write_config(self, edit))
        self.assertEqual(tray._match_mode_command("Go dark!"), "offline")
        self.assertEqual(tray._match_mode_command("back on"), "online")
        self.assertIsNone(tray._match_mode_command("offline mode"))
        popup = _popup("go dark")
        with patch.object(tray, "_refresh_chat_popup_status"):
            popup._send()
        self.assertTrue(tray._is_manually_offline())
        popup._add_system.assert_called_once_with('Offline. Type or say "wake up" to resume.')

    def test_reminder_regex_from_the_data(self):
        def edit(d):
            d["commands"]["reminders"] = "^what'?s on my plate$"
        tray.configure(_write_config(self, edit))
        self.assertTrue(tray._is_reminder_command("What's on my plate?"))
        self.assertFalse(tray._is_reminder_command("reminders"))

    def test_model_route_sends_study_to_the_model(self):
        def edit(d):
            d["commands"]["route"]["study"] = "model"
        tray.configure(_write_config(self, edit))
        popup = _popup("study mode", brain=Mock())
        with patch.object(tray, "_start_study_mode") as start, \
                patch.object(tray, "_match_study_command", return_value="study"), \
                patch.object(tray, "_set_thinking"), patch.object(tray, "_stop_speaking"), \
                patch.object(tray.conversation, "replay", return_value=[]), \
                patch.object(tray.threading, "Thread") as thread:
            self.assertEqual(popup._send(), "break")
        start.assert_not_called()
        thread.assert_called_once()  # the brain worker
        self.assertEqual(popup.messages[-1], {"role": "user", "content": "study mode"})

    def test_model_route_falls_back_to_the_tray_without_a_model(self):
        def edit(d):
            d["commands"]["route"]["study"] = "model"
            d["commands"]["route"]["reminders"] = "model"
        tray.configure(_write_config(self, edit))
        with patch.object(tray, "_start_study_mode") as start, \
                patch.object(tray, "_match_study_command", return_value="study"):
            _popup("study mode", brain=None)._send()
        start.assert_called_once()
        with patch.object(tray, "_show_reminder_card") as card, \
                patch.object(tray, "_match_study_command", return_value=None):
            _popup("reminders", brain=None)._send()
        card.assert_called_once()

    def test_wording_and_numbers_from_the_data(self):
        def edit(d):
            d["text"]["menu"]["talk"] = "Chat"
            d["text"]["menu"]["study_idle"] = "Pomodoro {study_min}+{rest_min}"
            d["text"]["status"]["online"] = "Up"
            d["chat"]["width"] = 400
            d["timing"]["thinking_max_ms"] = 5000
        tray.configure(_write_config(self, edit))
        self.assertEqual(tray._build_menu().items[0].text, "Chat")
        with patch.object(tray, "_study_mode", None):
            self.assertEqual(tray._study_menu_text(None), "Pomodoro 25+5")
        self.assertEqual(tray._status_display({"connected": True, "model_ready": True})[1], "Up")
        self.assertEqual(tray.CHAT_WIDTH, 400)
        self.assertEqual(tray.THINKING_MAX_MS, 5000)

    def test_bad_values_fall_back_with_a_log_line(self):
        def edit(d):
            d["commands"]["route"]["study"] = "sometimes"
            d["commands"]["reminders"] = "(unclosed"
            d["commands"]["online_phrases"] = [" "]
            d["colours"]["popup_bg"] = "black"
            d["text"]["chat"]["went_offline"] = "Offline {nope}"
        lines = []
        with patch.object(tray.json_config.log, "warning", lines.append):
            cfg = tray.configure(_write_config(self, edit))
        text = "\n".join(lines)
        for key in ("route.study", "commands.reminders", "online_phrases", "popup_bg", "went_offline"):
            self.assertIn(key, text)
        with open(tray.DEFAULTS_FILE, encoding="utf-8") as f:
            defaults = _strip(json.load(f))
        self.assertEqual(cfg["commands"], defaults["commands"])
        self.assertEqual(tray.POPUP_BG, "#050308")


if __name__ == "__main__":
    unittest.main()
