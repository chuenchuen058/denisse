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

Run:
    python optimize.py
"""

import json
import os
import shutil
import time

import psutil

try:
    import winreg
except ImportError:  # non-Windows -- startup-program management just no-ops
    winreg = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLEANUP_LOG_FILE = os.path.join(BASE_DIR, "memory", "cleanup_log.json")
DISABLED_STARTUP_DIR = os.path.join(BASE_DIR, "memory", "disabled_startup")
DISABLED_REGISTRY_FILE = os.path.join(BASE_DIR, "memory", "disabled_startup_registry.json")

TEMP_DIR = os.environ.get("TEMP") or os.path.join(os.path.expanduser("~"), "AppData", "Local", "Temp")
TEMP_QUIET_SECONDS = 24 * 60 * 60  # only remove temp files untouched this long
TEMP_POLL_SECONDS = 60 * 60  # clean once an hour

RESOURCE_POLL_SECONDS = 5 * 60  # check disk/memory every 5 minutes
LOW_DISK_FREE_GB = 5.0
LOW_DISK_FREE_PERCENT = 10.0
HIGH_MEMORY_PERCENT = 90.0

STARTUP_RUN_KEYS = (
    [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ]
    if winreg
    else []
)
STARTUP_FOLDER = os.path.join(
    os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup"
)


def _log(log_file: str, entry: dict) -> None:
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    log = []
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                log = json.load(f)
        except (json.JSONDecodeError, OSError):
            log = []
    log.append(entry)
    log = log[-500:]  # keep it bounded
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)


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
        drive = (os.environ.get("SystemDrive") or "C:") + "\\"
        total, _, free = shutil.disk_usage(drive)
        free_gb, total_gb = free / 1024**3, total / 1024**3
        lines.append(f"Disk ({drive}): {free_gb:.1f} GB free of {total_gb:.1f} GB ({100 * free / total:.0f}% free)")
    except OSError as e:
        lines.append(f"Disk: Error ({e})")

    try:
        mem = psutil.virtual_memory()
        lines.append(f"Memory: {mem.percent:.0f}% used ({mem.used / 1024**3:.1f} GB of {mem.total / 1024**3:.1f} GB)")
    except Exception as e:
        lines.append(f"Memory: Error ({e})")

    try:
        lines.append(f"CPU: {psutil.cpu_percent(interval=0.5):.0f}% used")
    except Exception as e:
        lines.append(f"CPU: Error ({e})")

    return "\n".join(lines)


def check_resource_warnings() -> list:
    """Return a list of short warning strings if disk space or memory
    usage has crossed a concerning threshold, else an empty list. Used by
    the background loop to decide whether to notify -- not itself an LLM
    tool (get_system_status is the on-demand, always-reports-everything
    version of this)."""
    warnings = []
    try:
        drive = (os.environ.get("SystemDrive") or "C:") + "\\"
        total, _, free = shutil.disk_usage(drive)
        free_gb, pct_free = free / 1024**3, 100 * free / total
        if free_gb < LOW_DISK_FREE_GB or pct_free < LOW_DISK_FREE_PERCENT:
            warnings.append(f"Low disk space on {drive}: {free_gb:.1f} GB free ({pct_free:.0f}%)")
    except OSError:
        pass
    try:
        pct = psutil.virtual_memory().percent
        if pct > HIGH_MEMORY_PERCENT:
            warnings.append(f"High memory usage: {pct:.0f}%")
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
                    entries.append(f"{name} (registry): {value}")
                    i += 1
        except OSError:
            continue
    if os.path.isdir(STARTUP_FOLDER):
        for name in os.listdir(STARTUP_FOLDER):
            if name.lower() == "desktop.ini":
                continue  # a folder-view settings file, not a program
            entries.append(f"{name} (Startup folder shortcut)")
    return "\n".join(entries) if entries else "No startup programs found."


def _load_registry_backup() -> dict:
    if not os.path.exists(DISABLED_REGISTRY_FILE):
        return {}
    try:
        with open(DISABLED_REGISTRY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_registry_backup(backup: dict) -> None:
    os.makedirs(os.path.dirname(DISABLED_REGISTRY_FILE), exist_ok=True)
    with open(DISABLED_REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=2)


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
                    return f"Disabled '{fname}'. Restore it any time with restore_startup_program."
                except OSError as e:
                    return f"Error disabling '{fname}': {e}"

    for hive, subkey in STARTUP_RUN_KEYS:
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_ALL_ACCESS) as key:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                backup = _load_registry_backup()
                backup[name] = {"hive": "HKCU" if hive == winreg.HKEY_CURRENT_USER else "HKLM", "value": value}
                _save_registry_backup(backup)
                winreg.DeleteValue(key, name)
                return f"Disabled startup entry '{name}'. Restore it any time with restore_startup_program."
        except OSError:
            continue

    return f"No startup program named '{name}' found (see list_startup_programs)."


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
            return f"Restored '{os.path.basename(disabled_path)}' to the Startup folder."
        except OSError as e:
            return f"Error restoring '{name}': {e}"

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
                return f"Restored startup entry '{name}'."
            except OSError as e:
                return f"Error restoring '{name}': {e}"

    return f"No disabled startup program named '{name}' found to restore."


def main() -> None:
    print("Denisse optimize -- one-off run:")
    print(get_system_status())
    print()
    cleaned = clean_temp_files()
    print(f"Temp cleanup: removed {len(cleaned)} file(s).")
    print()
    print("Startup programs:")
    print(list_startup_programs())


if __name__ == "__main__":
    main()
