"""
Denisse -- background file organizer.

Watches one or more folders (Downloads by default) and moves loose files
into type subfolders (Documents, Images, Videos, Audio, Archives,
Installers, Other by default) as they show up.

Deliberately NOT LLM-driven: this runs unattended with nobody watching a
confirmation prompt (see safety.py), so it sticks to a small deterministic
ruleset instead of letting a model decide what to run. Rules:

- Only moves loose FILES sitting directly in a watched folder -- never
  touches subfolders (including the category folders it creates), so it
  won't reorganize things you've already sorted by hand.
- Never deletes or overwrites anything. A same-named collision gets a
  " (2)", " (3)", ... suffix instead of clobbering the existing file.
- Leaves a file alone until it's been untouched for QUIET_SECONDS, and
  skips known partial-download extensions (.crdownload, .part, .tmp) --
  so it won't grab a file mid-download.
- Every move is appended to memory/organize_log.json so Denisse can tell
  you what she's done (e.g. via a future "what did you organize today"
  skill) and so you can audit or undo anything by hand.

The folders, categories, timings, limits and wording are data: the
"organize" section of housekeeping.json (AGENTS.md Rule 0).

Run:
    python auto_organize.py
"""

import json
import os
import shutil
import time
import threading

import housekeeping

_organize_lock = threading.RLock()

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = {}


def configure(path=None) -> dict:
    """(Re)load housekeeping.json's "organize" section into the module values."""
    global CONFIG, WATCH_FOLDERS, POLL_SECONDS, QUIET_SECONDS, SKIP_SUFFIXES, CATEGORIES, LOG_FILE
    CONFIG = housekeeping.load(path)["organize"]
    WATCH_FOLDERS = [housekeeping.path(f) for f in CONFIG["watch_folders"]]
    POLL_SECONDS = CONFIG["poll_s"]
    QUIET_SECONDS = CONFIG["quiet_s"]  # leave a file alone until it's been untouched this long
    SKIP_SUFFIXES = tuple(s.lower() for s in CONFIG["skip_suffixes"])
    CATEGORIES = {c["folder"]: {e.lower() for e in c["extensions"]} for c in CONFIG["categories"]}
    LOG_FILE = housekeeping.path(CONFIG["log_file"])
    return CONFIG


def _text(key, **fields) -> str:
    return CONFIG["text"][key].format(**fields)


configure()


def _category_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    for category, exts in CATEGORIES.items():
        if ext in exts:
            return category
    return CONFIG["other_folder"]


def _log_move(entry: dict) -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                log = json.load(f)
        except (json.JSONDecodeError, OSError):
            log = []
    log.append(entry)
    log = log[-CONFIG["log_keep"]:]  # keep it bounded
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)


def _unique_destination(dest_dir: str, name: str) -> str:
    dest_path = os.path.join(dest_dir, name)
    if not os.path.exists(dest_path):
        return dest_path
    base, ext = os.path.splitext(name)
    pattern = CONFIG["collision_name"]
    n = CONFIG["first_collision_number"]
    while os.path.exists(os.path.join(dest_dir, pattern.format(base=base, n=n, ext=ext))):
        n += 1
    return os.path.join(dest_dir, pattern.format(base=base, n=n, ext=ext))


def organize_folder(folder: str) -> list:
    """Run one pass over `folder`. Returns the list of moves made."""
    with _organize_lock:
        return _organize_folder(folder)


def _organize_folder(folder: str) -> list:
    if not os.path.isdir(folder):
        return []

    moved = []
    now = time.time()

    for name in os.listdir(folder):
        path = os.path.join(folder, name)

        if not os.path.isfile(path):
            continue  # leave subfolders alone, including our own category folders
        if name.startswith(tuple(CONFIG["skip_name_prefixes"])):
            continue
        if any(name.lower().endswith(suf) for suf in SKIP_SUFFIXES):
            continue  # still downloading

        try:
            st = os.stat(path)
            attrs = getattr(st, "st_file_attributes", 0)
            if attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                continue  # e.g. desktop.ini -- a folder-view settings file, not your file
            if now - st.st_mtime < QUIET_SECONDS:
                continue  # just written -- give it time to settle
            if os.path.islink(path):
                continue
        except OSError:
            continue

        category = _category_for(name)
        dest_dir = os.path.join(folder, category)
        # Existing junction/category links must not send moves outside the
        # watched folder. Leave these files alone rather than following them.
        if os.path.commonpath([os.path.realpath(folder), os.path.realpath(dest_dir)]) != os.path.realpath(folder):
            continue
        dest_path = _unique_destination(dest_dir, name)

        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(path, dest_path)
        except OSError as e:
            print(_text("move_error", name=name, error=e))
            continue

        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "file": name,
            "from": folder,
            "to": dest_path,
        }
        _log_move(entry)
        moved.append(entry)
        print(_text("moved", name=name, category=category))

    return moved


def main():
    print(_text("watching"))
    for folder in WATCH_FOLDERS:
        print(_text("watch_item", folder=folder))
    print(_text("stop_hint"))
    try:
        while True:
            for folder in WATCH_FOLDERS:
                organize_folder(folder)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print(_text("stopped"))


if __name__ == "__main__":
    main()
