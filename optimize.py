"""
Denisse -- background laptop optimization: temp-file cleanup and resource
monitoring, plus on-demand startup-program management.

Same reasoning as auto_organize.py for the automatic parts (temp cleanup,
resource checks): this runs unattended with nobody watching a confirmation
prompt, so it sticks to a small deterministic ruleset instead of letting a
model decide what to delete. Startup-program changes are NOT automatic --
they only happen when explicitly called (as a tool Denisse's model can use,
gated through the normal confirmation flow in safety.py, since disabling
something at login is more consequential and worth a human glance).

Thresholds, timings, paths, the startup locations and the wording are
data: the "optimize" section of housekeeping.json (AGENTS.md Rule 0).

Run:
    python optimize.py
"""

import os
import shutil
import time

import psutil

try:
    import winreg
except ImportError:  # non-Windows -- startup-program management just no-ops
    winreg = None

import housekeeping
import safe_json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = {}
_HIVES = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE"}  # names stored in the backup file


def configure(path=None) -> dict:
    """(Re)load housekeeping.json's "optimize" section into the module values."""
    global CONFIG, CLEANUP_LOG_FILE, DISABLED_STARTUP_DIR, DISABLED_REGISTRY_FILE, TEMP_DIR
    global TEMP_QUIET_SECONDS, TEMP_POLL_SECONDS, RESOURCE_POLL_SECONDS
    global LOW_DISK_FREE_GB, LOW_DISK_FREE_PERCENT, HIGH_MEMORY_PERCENT, STARTUP_RUN_KEYS, STARTUP_FOLDER
    CONFIG = cfg = housekeeping.load(path)["optimize"]
    CLEANUP_LOG_FILE = housekeeping.path(cfg["cleanup_log_file"])
    DISABLED_STARTUP_DIR = housekeeping.path(cfg["disabled_startup_dir"])
    DISABLED_REGISTRY_FILE = housekeeping.path(cfg["disabled_registry_file"])
    TEMP_DIR = (housekeeping.path(cfg["temp_dir"]) if cfg["temp_dir"]
                else os.environ.get("TEMP") or housekeeping.path(cfg["temp_dir_fallback"]))
    TEMP_QUIET_SECONDS = cfg["temp_quiet_s"]  # only remove temp files untouched this long
    TEMP_POLL_SECONDS = cfg["temp_poll_s"]
    RESOURCE_POLL_SECONDS = cfg["resource_poll_s"]
    LOW_DISK_FREE_GB = cfg["low_disk_free_gb"]
    LOW_DISK_FREE_PERCENT = cfg["low_disk_free_percent"]
    HIGH_MEMORY_PERCENT = cfg["high_memory_percent"]
    STARTUP_RUN_KEYS = ([(getattr(winreg, _HIVES[h]), cfg["startup_run_key"]) for h in cfg["startup_hives"]]
                        if winreg else [])
    STARTUP_FOLDER = os.path.join(os.environ.get("APPDATA", ""), cfg["startup_folder"])
    return CONFIG


def _text(key, **fields) -> str:
    return CONFIG["text"][key].format(**fields)


def _system_drive() -> str:
    return (CONFIG["system_drive"] or os.environ.get("SystemDrive") or CONFIG["system_drive_fallback"]) + "\\"


configure()


def _log(log_file: str, entry: dict) -> None:
    with safe_json.locked(log_file):
        log = safe_json.read_json(log_file, [])
        log.append(entry)
        log = log[-CONFIG["log_keep"]:]  # keep it bounded
        safe_json.write_json_atomic(log_file, log, indent=2)


def clean_temp_files() -> list:
    """Delete files in the user's Temp folder untouched for 24h+ (never
    anything newer, so nothing mid-use gets grabbed), and remove now-empty
    subfolders left behind. Every deletion is appended to
    memory/cleanup_log.json. Returns the list of files removed."""
    if not os.path.isdir(TEMP_DIR):
        return []
    now = time.time()
    cleaned = []
    for root, dirs, files in os.walk(TEMP_DIR, topdown=False):
        for name in files:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
                if now - st.st_mtime < TEMP_QUIET_SECONDS:
                    continue  # too recent -- might still be in use
                size = st.st_size
                os.remove(path)
                cleaned.append({"path": path, "size": size})
            except OSError:
                continue  # in use / permission denied -- skip, not fatal
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))  # only succeeds if empty
            except OSError:
                pass

    if cleaned:
        _log(
            CLEANUP_LOG_FILE,
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "files_removed": len(cleaned),
                "bytes_freed": sum(c["size"] for c in cleaned),
            },
        )
    return cleaned


def get_system_status() -> str:
    """Report disk space, memory, and CPU usage in one readable summary."""
    lines = []
    try:
        drive = _system_drive()
        total, _, free = shutil.disk_usage(drive)
        free_gb, total_gb = free / 1024**3, total / 1024**3
        lines.append(_text("disk", drive=drive, free_gb=free_gb, total_gb=total_gb, pct_free=100 * free / total))
    except OSError as e:
        lines.append(_text("disk_error", error=e))

    try:
        mem = psutil.virtual_memory()
        lines.append(_text("memory", percent=mem.percent, used_gb=mem.used / 1024**3, total_gb=mem.total / 1024**3))
    except Exception as e:
        lines.append(_text("memory_error", error=e))

    try:
        lines.append(_text("cpu", percent=psutil.cpu_percent(interval=CONFIG["cpu_sample_s"])))
    except Exception as e:
        lines.append(_text("cpu_error", error=e))

    return "\n".join(lines)


def check_resource_warnings() -> list:
    """Return a list of short warning strings if disk space or memory
    usage has crossed a concerning threshold, else an empty list. Used by
    the background loop to decide whether to notify -- not itself an LLM
    tool (get_system_status is the on-demand, always-reports-everything
    version of this)."""
    warnings = []
    try:
        drive = _system_drive()
        total, _, free = shutil.disk_usage(drive)
        free_gb, pct_free = free / 1024**3, 100 * free / total
        if free_gb < LOW_DISK_FREE_GB or pct_free < LOW_DISK_FREE_PERCENT:
            warnings.append(_text("low_disk", drive=drive, free_gb=free_gb, pct_free=pct_free))
    except OSError:
        pass
    try:
        pct = psutil.virtual_memory().percent
        if pct > HIGH_MEMORY_PERCENT:
            warnings.append(_text("high_memory", percent=pct))
    except Exception:
        pass
    return warnings


def list_startup_programs() -> str:
    """List programs configured to launch at login (Windows registry Run
    keys + the current user's Startup folder). Read-only."""
    entries = []
    for hive, subkey in STARTUP_RUN_KEYS:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    entries.append(_text("startup_registry", name=name, value=value))
                    i += 1
        except OSError:
            continue
    if os.path.isdir(STARTUP_FOLDER):
        for name in os.listdir(STARTUP_FOLDER):
            if name.lower() in {n.lower() for n in CONFIG["skip_startup_names"]}:
                continue  # e.g. desktop.ini -- a folder-view settings file, not a program
            entries.append(_text("startup_folder", name=name))
    return "\n".join(entries) if entries else _text("no_startup")


def _load_registry_backup() -> dict:
    """The saved Run-key values; {} if none. A damaged file is copied aside
    by safe_json -- it is the only record of how to restore a program."""
    return safe_json.read_json(DISABLED_REGISTRY_FILE, {})


def _save_registry_backup(backup: dict) -> None:
    safe_json.write_json_atomic(DISABLED_REGISTRY_FILE, backup, indent=2)


def disable_startup_program(name: str) -> str:
    """Disable a startup program by name -- a Startup-folder shortcut gets
    moved (not deleted) to memory/disabled_startup/; a registry Run-key
    entry gets backed up to memory/disabled_startup_registry.json and then
    removed. Always reversible with restore_startup_program."""
    if os.path.isdir(STARTUP_FOLDER):
        for fname in os.listdir(STARTUP_FOLDER):
            if name.lower() in (fname.lower(), os.path.splitext(fname)[0].lower()):
                os.makedirs(DISABLED_STARTUP_DIR, exist_ok=True)
                try:
                    shutil.move(os.path.join(STARTUP_FOLDER, fname), os.path.join(DISABLED_STARTUP_DIR, fname))
                    return _text("disabled_file", name=fname)
                except OSError as e:
                    return _text("disable_error", name=fname, error=e)

    for hive, subkey in STARTUP_RUN_KEYS:
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_ALL_ACCESS) as key:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                with safe_json.locked(DISABLED_REGISTRY_FILE):
                    backup = _load_registry_backup()
                    backup[name] = {"hive": "HKCU" if hive == winreg.HKEY_CURRENT_USER else "HKLM", "value": value}
                    _save_registry_backup(backup)
                winreg.DeleteValue(key, name)
                return _text("disabled_registry", name=name)
        except OSError:
            continue

    return _text("disable_not_found", name=name)


def restore_startup_program(name: str) -> str:
    """Undo a previous disable_startup_program call by name."""
    disabled_path = os.path.join(DISABLED_STARTUP_DIR, name) if os.path.isdir(DISABLED_STARTUP_DIR) else None
    if disabled_path and not os.path.exists(disabled_path):
        # allow restoring by base name without the original extension
        matches = [f for f in os.listdir(DISABLED_STARTUP_DIR) if os.path.splitext(f)[0].lower() == name.lower()]
        disabled_path = os.path.join(DISABLED_STARTUP_DIR, matches[0]) if matches else None
    if disabled_path and os.path.exists(disabled_path):
        try:
            shutil.move(disabled_path, os.path.join(STARTUP_FOLDER, os.path.basename(disabled_path)))
            return _text("restored_file", name=os.path.basename(disabled_path))
        except OSError as e:
            return _text("restore_error", name=name, error=e)

    with safe_json.locked(DISABLED_REGISTRY_FILE):
        backup = _load_registry_backup()
        if name in backup:
            entry = backup.pop(name)
            hive = winreg.HKEY_CURRENT_USER if entry["hive"] == "HKCU" else winreg.HKEY_LOCAL_MACHINE
            for h, subkey in STARTUP_RUN_KEYS:
                if h != hive:
                    continue
                try:
                    with winreg.OpenKey(h, subkey, 0, winreg.KEY_ALL_ACCESS) as key:
                        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, entry["value"])
                    _save_registry_backup(backup)
                    return _text("restored_registry", name=name)
                except OSError as e:
                    return _text("restore_error", name=name, error=e)

    return _text("restore_not_found", name=name)


def main() -> None:
    print(_text("run_title"))
    print(get_system_status())
    print()
    cleaned = clean_temp_files()
    print(_text("run_cleaned", count=len(cleaned)))
    print()
    print(_text("run_startup"))
    print(list_startup_programs())


if __name__ == "__main__":
    main()
