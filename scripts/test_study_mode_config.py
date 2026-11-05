"""study_mode.json drives the study clock's timings and words, and
tool_router.json the router's machine words: change the data, change the
behaviour (AGENTS.md Rule 0). The default behaviour itself is covered by
scripts/test_study_tools.py, scripts/test_study_command.py and
eval/test_router.py. Headless -- no Tk window, no chime, no model.

    .venv\\Scripts\\python.exe scripts\\test_study_mode_config.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import study_mode  # noqa: E402
import tool_router  # noqa: E402


def _strip(d):
    if isinstance(d, dict):
        return {k: _strip(v) for k, v in d.items() if not k.startswith("_")}
    return d


def _write(test, source, edit) -> str:
    with open(source, encoding="utf-8") as f:
        data = json.load(f)
    edit(data)
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    test.addCleanup(lambda: (os.remove(path), os.rmdir(folder)))
    return path


class NoTkRoot:
    def after(self, ms, fn):
        self.last_ms = ms
        return 1

    def after_cancel(self, job):
        pass


def user(text):
    return {"role": "user", "content": text}


class Shipped(unittest.TestCase):
    def test_shipped_files_equal_defaults(self):
        for mod in (study_mode, tool_router):
            with open(mod.CONFIG_FILE, encoding="utf-8") as a, open(mod.DEFAULTS_FILE, encoding="utf-8") as b:
                self.assertEqual(_strip(json.load(a)), _strip(json.load(b)), mod.__name__)

    def test_defaults_match_the_old_constants(self):
        self.assertEqual((study_mode.STUDY_MIN, study_mode.REST_MIN), (25, 5))
        self.assertEqual((study_mode.CHECK_MS, study_mode.BUBBLE_MS, study_mode.BUBBLE_W), (250, 90_000, 260))
        self.assertEqual((study_mode.BADGE_W, study_mode.BADGE_H, study_mode.BADGE_GAP, study_mode.BADGE_TUCK),
                         (96, 24, 4, 5))


class StudyData(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(study_mode, "_chime", lambda: None),
                        patch.object(study_mode, "_CURRENT", None),
                        patch.object(study_mode, "_dispatch", None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        study_mode.configure(study_mode.CONFIG_FILE)

    def test_timings_come_from_the_data(self):
        def edit(d):
            d["timings"]["study_min"] = 50
            d["timings"]["rest_min"] = 10
            d["timings"]["check_ms"] = 1000
        study_mode.configure(_write(self, study_mode.CONFIG_FILE, edit))
        root = NoTkRoot()
        mode = study_mode.StudyMode(root, lambda *a, **k: None)
        started = study_mode.start_session()
        self.assertIn("50 minutes of study, then a 10-minute rest", started)
        self.assertEqual(mode.minutes_left(), 50)
        self.assertEqual(root.last_ms, 1000)
        mode._enter("rest")
        self.assertEqual(mode.minutes_left(), 10)

    def test_wording_comes_from_the_data(self):
        def edit(d):
            d["wording"]["off"] = "Clock's idle."
            d["wording"]["context"] = "Timer: {state}"
            d["phases"]["study"]["doing"] = "heads down"
        study_mode.configure(_write(self, study_mode.CONFIG_FILE, edit))
        self.assertEqual(study_mode.session_status(), "No study session is running (study mode isn't set up in this process).")
        mode = study_mode.StudyMode(NoTkRoot(), lambda *a, **k: None)
        self.assertEqual(study_mode.session_status(), "Clock's idle.")
        mode.start()
        line = study_mode.context_line()
        self.assertTrue(line.startswith("Timer: Study session running: heads down,"), line)

    def test_politeness_strip_comes_from_the_data(self):
        self.assertEqual(study_mode.match_command("please start pomodoro", active=False), "study")
        study_mode.configure(_write(self, study_mode.CONFIG_FILE,
                                    lambda d: d.update(command_strip="^(?:kindly\\s+)")))
        self.assertIsNone(study_mode.match_command("please start pomodoro", active=False))
        self.assertEqual(study_mode.match_command("kindly start pomodoro", active=False), "study")

    def test_bad_values_fall_back_to_the_defaults(self):
        warnings = []

        def edit(d):
            d["timings"]["study_min"] = -3
            d["command_strip"] = "(unclosed"
            d["wording"]["paused"] = "{nonsense} left"
        with patch.object(study_mode.json_config.log, "warning", warnings.append):
            study_mode.configure(_write(self, study_mode.CONFIG_FILE, edit))
        self.assertEqual(study_mode.STUDY_MIN, 25)
        self.assertEqual(study_mode.match_command("please start pomodoro", active=False), "study")
        self.assertIn("{left}", study_mode.CONFIG["wording"]["paused"])
        self.assertEqual(len(warnings), 3, warnings)


class RouterData(unittest.TestCase):
    def tearDown(self):
        tool_router.configure(tool_router.CONFIG_FILE)

    def test_device_words_come_from_the_data(self):
        convo = [user("find my invoice file"), user("im pretty sure its in my laptop try again")]
        default = tool_router.matched_groups(convo)
        self.assertNotIn("system", default)
        tool_router.configure(_write(self, tool_router.CONFIG_FILE,
                                     lambda d: d.update(generic_device_words=["desktop"])))
        self.assertNotEqual(tool_router.matched_groups(convo), default)


if __name__ == "__main__":
    unittest.main(verbosity=2)
