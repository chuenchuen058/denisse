"""settings.py: every path and name follows config/env, else is discovered,
else fails with a message saying what to set. Temp folders only.

    .venv\\Scripts\\python.exe scripts\\test_settings.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings  # noqa: E402

KEYS = ("DENISSE_VAULT", "DENISSE_HOME", "DENISSE_REPO", "DENISSE_USER_NAME", "DENISSE_CONFIG", "APPDATA")


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in KEYS}
    env.update(extra)
    return env


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.json"
        self.appdata = self.tmp / "appdata"  # empty: no Obsidian registry

    def write_cfg(self, **values):
        self.cfg.write_text(json.dumps({"settings": values}), encoding="utf-8")

    def env(self, **extra):
        return mock.patch.dict(os.environ, _clean_env(DENISSE_CONFIG=str(self.cfg),
                                                      APPDATA=str(self.appdata), **extra), clear=True)

    def test_config_drives_every_value(self):
        home = self.tmp / "h"
        self.write_cfg(home=str(home), vault="~/Notes/V", repo="~/code/d",
                       user_name="Alex Example", user_about="a tester",
                       read_dirs=["{vault}", "~/Docs"], learning_note="L.md")
        with self.env():
            self.assertEqual(settings.home(), os.path.normpath(str(home)))
            self.assertEqual(settings.vault(), os.path.normpath(str(home / "Notes" / "V")))
            self.assertEqual(settings.repo(), os.path.normpath(str(home / "code" / "d")))
            self.assertEqual(settings.user_name(), "Alex Example")
            self.assertEqual([settings.expand(d) for d in settings.get("read_dirs")],
                             [os.path.normpath(str(home / "Notes" / "V")), os.path.normpath(str(home / "Docs"))])

    def test_env_beats_config(self):
        self.write_cfg(vault=str(self.tmp / "cfgvault"), user_name="Config Name")
        with self.env(DENISSE_VAULT=str(self.tmp / "envvault"), DENISSE_USER_NAME="Env Name",
                      DENISSE_HOME=str(self.tmp / "envhome"), DENISSE_REPO=str(self.tmp / "envrepo")):
            self.assertEqual(settings.vault(), str(self.tmp / "envvault"))
            self.assertEqual(settings.user_name(), "Env Name")
            self.assertEqual(settings.home(), os.path.normpath(str(self.tmp / "envhome")))
            self.assertEqual(settings.repo(), os.path.normpath(str(self.tmp / "envrepo")))

    def test_discovers_obsidian_vault_open_first(self):
        reg = self.appdata / "obsidian"
        reg.mkdir(parents=True)
        (reg / "obsidian.json").write_text(json.dumps({"vaults": {
            "a": {"path": str(self.tmp / "newer"), "ts": 9},
            "b": {"path": str(self.tmp / "open"), "ts": 1, "open": True}}}), encoding="utf-8")
        with self.env():  # no config file at all
            self.assertEqual(settings.vault(), os.path.normpath(str(self.tmp / "open")))
            self.assertEqual(settings.repo(), str(ROOT))
            self.assertEqual(settings.home(), str(Path.home()))

    def test_nothing_found_says_what_to_set(self):
        self.write_cfg()
        with self.env(), mock.patch.object(settings, "_git_user", return_value=None):
            with self.assertRaises(settings.SettingsError) as e:
                settings.vault()
            self.assertIn("DENISSE_VAULT", str(e.exception))
            self.assertIn("config.example.json", str(e.exception))
            with self.assertRaises(settings.SettingsError) as e:
                settings.user_name()
            self.assertIn("DENISSE_USER_NAME", str(e.exception))

    def test_modules_follow_settings(self):
        """A fresh process: every switched module takes its paths from settings."""
        home, vault = self.tmp / "h", self.tmp / "h" / "V"
        self.write_cfg(home=str(home), user_name="Alex Example", user_about="a tester",
                       read_dirs=["{vault}", "~/Docs"], learning_note="Notes/How-I-Learn.md")
        code = (
            "import json, delegate, vault_search, instincts, obsidian_sync, planner\n"
            "print(json.dumps([delegate.VAULT, delegate.DRAFTS, delegate.READ_DIRS, delegate.SYSTEM,\n"
            "  delegate._prompt('x', []), vault_search.VAULT, instincts.VAULT, obsidian_sync.VAULT_DIR,\n"
            "  planner.VAULT]))\n")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                             env=_clean_env(DENISSE_CONFIG=str(self.cfg), DENISSE_VAULT=str(vault),
                                            APPDATA=str(self.appdata)), timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        d_vault, drafts, read_dirs, system, prompt, *others = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(d_vault, str(vault))
        self.assertEqual(drafts, os.path.join(str(vault), "Denisse", "Drafts"))
        self.assertEqual(read_dirs, [os.path.normpath(str(vault)), os.path.normpath(str(home / "Docs"))])
        self.assertTrue(system.startswith("You are doing a job for Alex Example, a tester, on behalf of Denisse"))
        self.assertIn("follow Notes/How-I-Learn.md.", prompt)
        self.assertEqual(others, [str(vault)] * 4)

    def test_install_capture_points_hooks_at_repo(self):
        home = self.tmp / "h"
        home.mkdir()
        out = subprocess.run([sys.executable, str(ROOT / "atrium" / "install_capture.py")],
                             capture_output=True, text=True, timeout=60,
                             env=_clean_env(USERPROFILE=str(home), HOME=str(home),
                                            DENISSE_REPO=str(self.tmp / "repo"),
                                            DENISSE_CONFIG=str(self.cfg)))
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        repo = (self.tmp / "repo").as_posix()
        self.assertEqual(cmd, f'"{repo}/.venv/Scripts/python.exe" "{repo}/atrium/hook.py"')


if __name__ == "__main__":
    unittest.main()
