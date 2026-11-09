"""Model names come from config.json (AGENTS.md Rule 0): changing the config changes the
model each consumer uses, and the existing env overrides still win. Offline; every config
here is a temp file, nothing is loaded or pulled.

    .venv\\Scripts\\python.exe scripts\\test_model_config.py
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "demo"))

import model_config  # noqa: E402

CHAT, WHISPER = "test-chat:1b", "test-whisper.en"


def _clean_env():
    """os.environ without the overrides, so the config file is what decides."""
    return {k: v for k, v in os.environ.items()
            if k not in ("DENISSE_MODEL", "DENISSE_WHISPER_MODEL", "DENISSE_CONFIG")}


class ConfigFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        real = json.loads(Path(model_config.__file__).with_name("config.json").read_text(encoding="utf-8"))
        real["models"]["chat"]["value"] = CHAT
        real["models"]["whisper"]["value"] = WHISPER
        self.path = Path(tmp.name) / "config.json"
        self.path.write_text(json.dumps(real), encoding="utf-8")
        self.env = dict(_clean_env(), DENISSE_CONFIG=str(self.path))

    def reload(self, name):
        """Re-import a module that reads its model at import, then put it back afterwards."""
        mod = importlib.import_module(name)
        saved = dict(os.environ)

        def restore():
            with patch.dict(os.environ, saved, clear=True):
                importlib.reload(mod)
        self.addCleanup(restore)
        return importlib.reload(mod)

    def test_config_value_used(self):
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(model_config.model("chat"), CHAT)
            self.assertEqual(model_config.model("whisper"), WHISPER)

    def test_env_override_still_wins(self):
        with patch.dict(os.environ, dict(self.env, DENISSE_MODEL="env-chat",
                                         DENISSE_WHISPER_MODEL="env-whisper"), clear=True):
            self.assertEqual(model_config.model("chat"), "env-chat")
            self.assertEqual(model_config.model("whisper"), "env-whisper")

    def test_empty_env_falls_back_to_config(self):
        with patch.dict(os.environ, dict(self.env, DENISSE_MODEL=""), clear=True):
            self.assertEqual(model_config.model("chat"), CHAT)

    def test_shipped_config_names_the_env_overrides(self):
        with patch.dict(os.environ, _clean_env(), clear=True):
            self.assertEqual(model_config.entry("chat")["env"], "DENISSE_MODEL")
            self.assertEqual(model_config.entry("whisper")["env"], "DENISSE_WHISPER_MODEL")

    def test_brain_default_matches_config_until_brain_reads_it(self):
        """brain.py (owned by another task) still has its own DENISSE_MODEL default. Until it
        calls model_config.model("chat"), the two must agree, or Atrium and the demo would
        name a model the brain isn't running. Delete this once brain.py reads the config."""
        import ast
        tree = ast.parse((ROOT / "brain.py").read_text(encoding="utf-8"))
        defaults = [n.args[1].value for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and len(n.args) == 2
                    and isinstance(n.args[0], ast.Constant) and n.args[0].value == "DENISSE_MODEL"
                    and isinstance(n.args[1], ast.Constant)]
        with patch.dict(os.environ, _clean_env(), clear=True):
            for d in defaults:
                self.assertEqual(d, model_config.model("chat"))

    def test_stt_whisper_follows_config(self):
        with patch.dict(os.environ, self.env, clear=True):
            stt = self.reload("stt_whisper")
        self.assertEqual(stt.MODEL_NAME, WHISPER)

    def test_bakeoff_baseline_follows_config(self):
        with patch.dict(os.environ, self.env, clear=True):
            mb = self.reload("model_bakeoff")
        self.assertEqual(mb.DEFAULT_MODEL, CHAT)
        self.assertIn(f"--models {CHAT},<candidate>", mb.render([], "now", "raw.json"))

    def test_bakeoff_candidates_come_from_the_data_file(self):
        import model_bakeoff as mb
        data = json.loads((ROOT / "scripts" / "model_bakeoff_candidates.json").read_text(encoding="utf-8"))
        self.assertEqual([c[0] for c in mb.CANDIDATES], [c["name"] for c in data["candidates"]])
        self.assertIn(f"ollama pull {mb.CANDIDATES[0][0]}", mb.render([], "now", "raw.json"))

    def test_atrium_shows_configured_models(self):
        from atrium import snapshot
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(snapshot.subprocess, "run", side_effect=OSError("no shell in tests")):
            role = next(r["role"] for r in snapshot._agents() if r["agent"] == "Denisse")
        self.assertIn(CHAT, role)
        self.assertIn(WHISPER, role)

    def test_demo_report_uses_reported_model_then_config(self):
        import report
        data = {"machine": {"model": "reported-by-run:2b"}, "git": {}, "parts": {}}
        res = Path(tempfile.mkdtemp()) / "results.json"  # own folder: report.previous() reads its siblings
        with patch.dict(os.environ, self.env, clear=True):
            self.assertIn("ollama pull reported-by-run:2b", report.render(data, res))
            data["machine"] = {}
            self.assertIn(f"ollama pull {CHAT}", report.render(data, res))


if __name__ == "__main__":
    unittest.main()
