"""The local team's model comes from orchestra/models.json (AGENTS.md Rule 0): changing that
file changes the model load_config() hands every role, and it wins over a stale
local_team_model left in config.json. Temp repos only; nothing calls Ollama.

    .venv\\Scripts\\python.exe -m unittest discover -s orchestra/local_team -p "test_models_config.py" -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from orchestra.local_team import core  # noqa: E402


class ModelsJson(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "orchestra").mkdir()

    def write(self, name, data):
        (self.repo / "orchestra" / name).write_text(json.dumps(data), encoding="utf-8")

    def test_models_json_sets_the_model(self):
        self.write("models.json", {"local_team_model": "test-local:1b"})
        self.assertEqual(core.load_config(self.repo)["local_team_model"], "test-local:1b")
        self.assertEqual(core.Ctx(self.repo).cfg["local_team_model"], "test-local:1b")

    def test_models_json_wins_over_config_json(self):
        self.write("config.json", {"local_team_model": "stale:1b", "local_team_timeout_s": 5})
        self.write("models.json", {"local_team_model": "test-local:1b"})
        cfg = core.load_config(self.repo)
        self.assertEqual(cfg["local_team_model"], "test-local:1b")
        self.assertEqual(cfg["local_team_timeout_s"], 5, "config.json still sets the limits")

    def test_falls_back_to_this_checkouts_models_json(self):
        shipped = json.loads((core.HERE.parent / "models.json").read_text(encoding="utf-8"))
        self.assertEqual(core.load_config(self.repo)["local_team_model"], shipped["local_team_model"])

    def test_no_model_literal_in_defaults(self):
        self.assertNotIn("local_team_model", core.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
