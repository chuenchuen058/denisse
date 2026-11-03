"""Where things are and who they're for: one source (AGENTS.md Rule 0).

    settings.vault()      # the Obsidian vault
    settings.home()       # the user's home folder
    settings.repo()       # this checkout
    settings.user_name()  # whose assistant this is
    settings.get("key")   # any other value in config.json's "settings"

Each value is resolved, first hit wins:

  1. its environment variable (DENISSE_VAULT, DENISSE_HOME, DENISSE_REPO,
     DENISSE_USER_NAME) -- env first, as model_config does, so a test that
     points DENISSE_VAULT at a temp folder can never be overridden by a
     config value into writing the real vault;
  2. config.json's "settings" section (DENISSE_CONFIG points at another
     copy; see config.example.json for every key). "~" in a path means
     settings.home();
  3. runtime discovery: the vault Obsidian itself has registered
     (%APPDATA%/obsidian/obsidian.json), the OS home folder, the folder this
     file is in, git's user.name.

If none of them gives a value, SettingsError says which knobs to set.
No machine-specific value lives in this file.
"""
import json
import os
import subprocess
from pathlib import Path

import model_config


class SettingsError(RuntimeError):
    pass


def _config() -> dict:
    path = model_config.config_path()
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    return {k: v for k, v in (data.get("settings") or {}).items() if not k.startswith("_")}


def get(key: str, default=None):
    """A raw value from config.json's "settings" (None/absent -> default)."""
    value = _config().get(key)
    return default if value is None else value


def _env(name: str):
    return os.environ.get(name) or None


def expand(path: str) -> str:
    """A config path as a real path: "~" is settings.home(), "{vault}" the vault."""
    if "{vault}" in path:
        path = path.replace("{vault}", vault())
    if path == "~" or path.startswith(("~/", "~\\")):
        path = os.path.join(home(), path[2:]) if len(path) > 1 else home()
    return os.path.normpath(path)


def home() -> str:
    found = _env("DENISSE_HOME") or get("home")
    if found:
        return os.path.normpath(os.path.expanduser(found))
    return str(Path.home())


def repo() -> str:
    found = _env("DENISSE_REPO") or get("repo")
    if found:
        return expand(found)
    return os.path.dirname(os.path.abspath(__file__))


def _obsidian_vault():
    """The vault Obsidian has registered: the open one, else the most recent."""
    appdata = os.environ.get("APPDATA") or os.path.join(home(), ".config")
    try:
        with open(os.path.join(appdata, "obsidian", "obsidian.json"), encoding="utf-8") as f:
            vaults = list((json.load(f).get("vaults") or {}).values())
    except (OSError, ValueError, AttributeError):
        return None
    vaults = [v for v in vaults if isinstance(v, dict) and v.get("path")]
    if not vaults:
        return None
    vaults.sort(key=lambda v: (bool(v.get("open")), v.get("ts") or 0), reverse=True)
    return vaults[0]["path"]


def vault() -> str:
    found = _env("DENISSE_VAULT")
    if found:
        return found  # verbatim, as every module used it before this file
    found = get("vault")
    if found:
        return expand(found)
    found = _obsidian_vault()
    if found:
        return os.path.normpath(found)
    raise SettingsError(
        "No Obsidian vault found: set DENISSE_VAULT, or \"vault\" under "
        "\"settings\" in config.json (see config.example.json), or open the "
        "vault once in Obsidian so it is registered.")


def _git_user():
    try:
        out = subprocess.run(["git", "config", "user.name"], capture_output=True,
                             text=True, timeout=5, cwd=repo())
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def user_name() -> str:
    found = _env("DENISSE_USER_NAME") or get("user_name") or _git_user()
    if found:
        return found
    raise SettingsError(
        "No user name: set DENISSE_USER_NAME, or \"user_name\" under "
        "\"settings\" in config.json (see config.example.json).")
