"""housekeeping.json: the data behind auto_organize, optimize, mission_control,
agent_loop and agent_tools (AGENTS.md Rule 0).

    housekeeping.load()            # the whole checked file
    housekeeping.path("memory/x")  # a configured path as a real path

Checked against housekeeping.defaults.json by json_config: a bad or missing
value logs one line and uses the shipped default. DENISSE_HOUSEKEEPING_CONFIG
points at another file (tests). Each module reads its own section in its
configure(), called once at import.
"""
import os

import json_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "housekeeping.json")
DEFAULTS_FILE = os.path.join(BASE_DIR, "housekeeping.defaults.json")
CONFIG_ENV_VAR = "DENISSE_HOUSEKEEPING_CONFIG"
SHORTCUT_TYPES = ("talk", "app", "path")  # the kinds mission_control.run_shortcut can launch


def _extra_checks(data: dict, defaults: dict, checker) -> dict:
    org = data["organize"]
    good = [c for c in org["categories"]
            if isinstance(c.get("folder"), str) and c["folder"].strip()
            and isinstance(c.get("extensions"), list)
            and all(isinstance(e, str) for e in c["extensions"])]
    if len(good) != len(org["categories"]):
        org["categories"] = checker.bad("housekeeping.organize.categories",
                                        "needs a folder name and a list of extensions in each entry",
                                        defaults["organize"]["categories"])
    for key in ("other_folder",):
        if not org[key].strip():
            org[key] = checker.bad(f"housekeeping.organize.{key}", "is empty", defaults["organize"][key])
    if not org["watch_folders"]:
        org["watch_folders"] = checker.bad("housekeeping.organize.watch_folders", "is empty",
                                           defaults["organize"]["watch_folders"])
    opt = data["optimize"]
    if not opt["startup_hives"] or set(opt["startup_hives"]) - {"HKCU", "HKLM"}:
        opt["startup_hives"] = checker.bad("housekeeping.optimize.startup_hives", "should list HKCU and/or HKLM",
                                           defaults["optimize"]["startup_hives"])
    mc = data["mission_control"]
    if not 0 < mc["port"] < 65536:
        mc["port"] = checker.bad("housekeeping.mission_control.port", "should be 1-65535",
                                 defaults["mission_control"]["port"])
    if any(not isinstance(s.get("label"), str) or s.get("type") not in SHORTCUT_TYPES
           or not isinstance(s.get("target", ""), str) for s in mc["default_shortcuts"]):
        mc["default_shortcuts"] = checker.bad("housekeeping.mission_control.default_shortcuts",
                                              f"needs a label and a type in {SHORTCUT_TYPES} in each entry",
                                              defaults["mission_control"]["default_shortcuts"])
    return data


def load(path=None) -> dict:
    """The checked contents of housekeeping.json (or `path`, or $DENISSE_HOUSEKEEPING_CONFIG)."""
    path = path or os.environ.get(CONFIG_ENV_VAR) or CONFIG_FILE
    return json_config.load("housekeeping", path, DEFAULTS_FILE, extra=_extra_checks)


def path(value: str) -> str:
    """A configured path: '~' and '{vault}' via settings.expand, relative = inside this checkout."""
    if value.startswith("~") or "{vault}" in value:
        import settings
        return settings.expand(value)
    return os.path.normpath(os.path.join(BASE_DIR, value))
