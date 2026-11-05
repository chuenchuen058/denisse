"""tools.json drives tools.py's limits, folders, word lists and wording, and
runtime.defaults.json drives standing_instructions.py's paths and wording:
the shipped data reproduces the old behaviour, changing the data changes it,
and a bad value logs and falls back. Temp files only; no model or network.

    .venv\\Scripts\\python.exe scripts\\test_tools_config.py
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runtime_config  # noqa: E402
import standing_instructions as standing  # noqa: E402
import tools  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="denisse-tools-config-"))


def _defaults():
    return json.loads(Path(tools.DEFAULTS_FILE).read_text(encoding="utf-8"))


class ToolsConfig(unittest.TestCase):
    def use(self, edit):
        """Point tools.py at a temp tools.json: the defaults with `edit` applied."""
        data = _defaults()
        edit(data)
        path = TMP / f"tools-{self.id().rsplit('.', 1)[-1]}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        p = patch.object(tools, "CONFIG_FILE", str(path))
        p.start()
        self.addCleanup(tools.reload)
        self.addCleanup(p.stop)
        tools.reload()

    def setUp(self):
        tools.reload()
        facts = TMP / f"facts-{self.id().rsplit('.', 1)[-1]}.json"
        p = patch.object(tools, "FACTS_FILE", str(facts))
        p.start()
        self.addCleanup(p.stop)
        self.sync = patch.object(tools.obsidian_sync, "sync_facts", lambda facts: None)
        self.sync.start()
        self.addCleanup(self.sync.stop)

    # -- the shipped data is the old behaviour ----------------------------
    def test_live_file_matches_shipped_defaults(self):
        live = json.loads(Path(tools.CONFIG_FILE).read_text(encoding="utf-8"))
        self.assertEqual(live, _defaults())

    def test_defaults_match_the_old_hardcoded_values(self):
        self.assertEqual(tools.calculate("9.9 - 9.11"), "9.9 - 9.11 = 0.79")
        self.assertEqual(tools.calculate("1/0"), "Error: division by zero.")
        self.assertEqual(tools._repo_path("facts"), os.path.join(str(ROOT), "memory", "facts.json"))
        self.assertEqual(tools.SKILLS_DIR, os.path.join(str(ROOT), "skills"))
        self.assertEqual(tools.find_files(""), "Error: give me something to search for.")
        self.assertEqual(tools._say("open_application", "opened", app_name="Chrome"), "Opened Chrome")
        self.assertEqual(tools.list_directory(str(TMP / "nothing-here-yet"))[:6], "Error:")
        (TMP / "empty.txt").write_text("", encoding="utf-8")
        self.assertEqual(tools.read_file(str(TMP / "empty.txt")), "(empty file)")

    def test_framing_trim_unchanged(self):
        trim = lambda t: tools._framing_pattern().sub("", t, count=1)  # noqa: E731
        self.assertEqual(trim("remind me there is a robotics meeting"), "robotics meeting")
        self.assertEqual(trim("Please remind me to email Ben"), "email Ben")
        self.assertEqual(trim("reminder for the dentist"), "the dentist")
        self.assertEqual(trim("remember that there's an exam"), "exam")
        self.assertEqual(trim("email Ben"), "email Ben")

    def test_recall_unchanged(self):
        tools.remember("favorite_editor", "VS Code")
        self.assertEqual(tools.recall("favourite editor"), "favorite_editor: VS Code")
        self.assertIn("filed under: favorite_editor", tools.recall("zzz"))

    # -- changing the data changes the behaviour ---------------------------
    def test_wording_comes_from_data(self):
        self.use(lambda d: d["calculate"].update(division_by_zero="cannot divide by nothing."))
        self.assertEqual(tools.calculate("1/0"), "Error: cannot divide by nothing.")
        self.use(lambda d: d["facts"].update(remembered="Saved {key}."))
        self.assertEqual(tools.remember("k", "v"), "Saved k.")

    def test_rounding_comes_from_data(self):
        self.use(lambda d: d["calculate"].update(round_digits=1))
        self.assertEqual(tools.calculate("2/3"), "2/3 = 0.7")

    def test_framing_comes_from_data(self):
        self.use(lambda d: d["tasks"]["framing"].update(asks=["nudge"]))
        trim = lambda t: tools._framing_pattern().sub("", t, count=1)  # noqa: E731
        self.assertEqual(trim("nudge me to call mum"), "call mum")
        self.assertEqual(trim("remind me to call mum"), "remind me to call mum")

    def test_recall_lists_come_from_data(self):
        tools.remember("favorite_editor", "VS Code")
        self.use(lambda d: d["facts"].update(spelling=[]))
        # Without the spelling fold, "favourite" is just another word; the
        # shared 5-letter prefix "favou"/"favor" doesn't match either.
        self.assertNotEqual(tools.recall("favourite"), "favorite_editor: VS Code")
        self.use(lambda d: d["facts"].update(spelling=[["favourite", "favorite"]]))
        self.assertEqual(tools.recall("favourite"), "favorite_editor: VS Code")

    def test_search_folders_and_limits_come_from_data(self):
        home = TMP / "home"
        (home / "Stuff").mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (home / "Stuff" / f"japan_flight_{i}.pdf").write_text("x", encoding="utf-8")
        self.use(lambda d: d["find_files"].update(search_folders=["Stuff"], max_results=2))
        with patch.object(tools.os.path, "expanduser", lambda p: str(home) if p == "~" else p):
            self.assertEqual(set(tools._default_search_dirs()), {"stuff"})
            got = tools.find_files("my japan flight")
        self.assertTrue(got.startswith("Found 3 match(es), showing 2"), got)
        self.assertIn("...and 1 more", got)

    def test_filler_words_come_from_data(self):
        home = TMP / "home2"
        (home / "Stuff").mkdir(parents=True, exist_ok=True)
        (home / "Stuff" / "japan_flight.pdf").write_text("x", encoding="utf-8")
        with patch.object(tools.os.path, "expanduser", lambda p: str(home) if p == "~" else p):
            self.use(lambda d: d["find_files"].update(search_folders=["Stuff"]))
            self.assertTrue(tools.find_files("my japan flight").startswith("Found 1"))
            self.use(lambda d: d["find_files"].update(search_folders=["Stuff"], filler_words=[]))
            self.assertTrue(tools.find_files("my japan flight").startswith("No files matching"))

    # -- bad data falls back ----------------------------------------------
    def test_bad_values_fall_back(self):
        def bad(d):
            d["calculate"]["round_digits"] = "ten"
            d["calculate"]["failed"] = "bad {nope}"
            d["tasks"]["framing"]["asks"] = []
        with self.assertLogs("denisse.json_config", "WARNING"):
            self.use(bad)
        self.assertEqual(tools.config()["calculate"]["round_digits"], 10)
        self.assertIn("couldn't evaluate", tools.calculate("1 +"))
        self.assertEqual(tools._framing_pattern().sub("", "remind me to x", count=1), "x")

    def test_unformattable_template_uses_default(self):
        self.use(lambda d: d["clean_temp"].update(removed="Removed {count:.2f} file(s), {mb:.1f} MB."))
        with self.assertLogs("denisse.tools", "WARNING"):
            said = tools._say("clean_temp", "removed", count="3", mb=1.0)
        self.assertEqual(said, "Removed 3 temp file(s), freeing 1.0 MB.")


class StandingInstructionsConfig(unittest.TestCase):
    def use(self, runtime):
        path = TMP / f"config-{self.id().rsplit('.', 1)[-1]}.json"
        path.write_text(json.dumps({"settings": {"runtime": runtime}}), encoding="utf-8")
        env = patch.dict(os.environ, {"DENISSE_CONFIG": str(path)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(runtime_config.reload)
        runtime_config.reload()

    def setUp(self):
        runtime_config.reload()

    def test_defaults_match_the_old_hardcoded_values(self):
        engine = standing.Engine(model=object())
        self.assertEqual(engine.config, standing.ROOT / "memory" / "standing_instructions.json")
        self.assertEqual(engine.state, standing.ROOT / "memory" / "agent_state.json")
        self.assertEqual(engine.reports, standing.ROOT / "logs" / "agent_runs")
        with self.assertRaisesRegex(ValueError, "^Invalid standing-instruction settings$"):
            standing.validate({})

    def test_paths_and_wording_come_from_data(self):
        self.use({"standing_instructions": {
            "files": {"config": "elsewhere/si.json"},
            "wording": {"invalid_settings": "Those settings don't make sense",
                        "failed_brief": "{job_id} failed ({error})"}}})
        engine = standing.Engine(model=object())
        self.assertEqual(engine.config, standing.ROOT / "elsewhere" / "si.json")
        self.assertEqual(engine.state, standing.ROOT / "memory" / "agent_state.json")
        with self.assertRaisesRegex(ValueError, "^Those settings don't make sense$"):
            standing.validate({})
        self.assertEqual(standing._say("failed_brief", job_id="downloads", error="x"), "downloads failed (x)")

    def test_failed_run_brief_uses_wording(self):
        self.use({"standing_instructions": {"wording": {"failed_brief": "{job_id} broke: {error}"}}})
        d = TMP / "si-failed"
        cfg = d / "si.json"
        d.mkdir(exist_ok=True)
        data = standing.defaults()
        data["enabled"] = True
        cfg.write_text(json.dumps(data), encoding="utf-8")
        engine = standing.Engine(config=cfg, state=d / "state.json", reports=d / "reports", model=object())
        with patch.object(standing.agent_tools, "organize_downloads", side_effect=RuntimeError("disk")):
            result = engine.run_job("downloads", now=1000.0)
        self.assertEqual(result["brief"], "downloads broke: RuntimeError: disk")

    def test_bad_wording_falls_back(self):
        with self.assertLogs("denisse.runtime_config", "WARNING"):
            self.use({"standing_instructions": {"wording": {"failed_brief": "{nope}"}}})
        self.assertEqual(standing._say("failed_brief", job_id="a", error="b"), "a: b")


if __name__ == "__main__":
    unittest.main()
