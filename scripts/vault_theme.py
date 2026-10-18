"""Keep the vault's three colour systems saying the same thing.

Obsidian colours the same folders in three unrelated places:
  .obsidian/graph.json                            -- graph node groups (rgb as int)
  .obsidian/plugins/color-folders-files/data.json  -- the file explorer (hex)
  .obsidian/appearance.json                       -- theme and accent

This script is the single source of truth for the first two, so a new folder
can't end up one colour in the sidebar and another in the graph. Run it with
OBSIDIAN CLOSED: Obsidian writes graph.json out when it exits and would
overwrite anything changed underneath it.

--write to apply; the default prints what would change.
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
V = r"C:\Users\Xichu\OneDrive - King's College London\Denisse"
WRITE = "--write" in sys.argv

GRAPH = os.path.join(V, ".obsidian", "graph.json")
FOLDERS = os.path.join(V, ".obsidian", "plugins", "color-folders-files", "data.json")
APPEARANCE = os.path.join(V, ".obsidian", "appearance.json")

ACCENT = "#a855f7"  # the orb's violet, so the app and Denisse match

# One hue per area. Order matters in the graph: a subfolder rule has to come
# BEFORE the parent rule that would otherwise swallow it.
AREAS = [
    ("SEM 1 Modules", "#fde047", True),
    ("AKC", "#fdba74", True),
    ("KCLMSoc", "#86efac", True),
    ("GitHub", "#67e8f9", True),
    ("Memory", "#93c5fd", True),
    ("Denisse/Evals", "#a5b4fc", False),
    ("Denisse/Brain", "#f0abfc", False),
    ("Denisse/Project Log", "#f9a8d4", False),
    ("Denisse", "#c4b5fd", True),
    ("Log", "#cbd5e1", True),
]
# Explorer-only styling: shades and emphasis the graph has no equivalent for.
EXTRA_FOLDERS = {
    "SEM 1 Modules/5CCYB010 Electromagnetism": {"textColor": "#fef08a"},
    "SEM 1 Modules/5CCYB020 Signals and Systems": {"textColor": "#fef08a"},
    "SEM 1 Modules/5CCYB070 Computational Methods (26~27 SY)": {"textColor": "#fef08a"},
    "SEM 1 Modules/5CCYB080 Computational Applied Biomathematics": {"textColor": "#fef08a"},
    "Denisse/Task Archive": {"textColor": "#cbd5e1", "italic": True},
    "Denisse/Vault Health.md": {"textColor": "#94a3b8", "italic": True},
    "Templates": {"textColor": "#64748b", "italic": True},
}


def to_int(hx):
    n = int(hx.lstrip("#"), 16)
    assert f"#{n:06x}" == hx.lower(), f"{hx} did not round-trip"
    return n


def load(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def save(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


# -- graph groups -----------------------------------------------------------
graph = load(GRAPH)
before = [(g["query"], g["color"]["rgb"]) for g in graph.get("colorGroups", [])]
graph["colorGroups"] = [
    {"query": f'path:"{p}"', "color": {"a": 1, "rgb": to_int(hx)}} for p, hx, _ in AREAS
]
after = [(g["query"], g["color"]["rgb"]) for g in graph["colorGroups"]]
print("graph.json")
for row in after:
    mark = " " if row in before else "+"
    print(f"  {mark} {row[0]:<34} {row[1]}  #{row[1]:06x}")
for row in before:
    if row not in after:
        print(f"  - {row[0]:<34} {row[1]}  #{row[1]:06x}")

# -- the file explorer ------------------------------------------------------
folders = load(FOLDERS)
old_styles = dict(folders.get("styles", {}))
styles = {}
for p, hx, bold in AREAS:
    e = {"textColor": hx}
    if bold:
        e["bold"] = True
    styles[p] = e
styles.update(EXTRA_FOLDERS)
# Keep only paths that still exist -- a deleted folder leaves dead config.
gone = [p for p in styles if not os.path.exists(os.path.join(V, p.replace("/", os.sep)))]
for p in gone:
    print(f"  (skipping {p}: not in the vault)")
    styles.pop(p)
folders["styles"] = styles
print("color-folders-files")
for p, e in styles.items():
    was = old_styles.get(p)
    mark = " " if was == e else ("+" if was is None else "~")
    print(f"  {mark} {p:<56} {e['textColor']}")
for p in old_styles:
    if p not in styles:
        print(f"  - {p:<56} {old_styles[p].get('textColor')}  (dropped)")

# -- accent -----------------------------------------------------------------
appearance = load(APPEARANCE)
print("appearance.json")
print(f"  accentColor {appearance.get('accentColor') or '(default)'} -> {ACCENT}")
appearance["accentColor"] = ACCENT

if WRITE:
    save(GRAPH, graph)
    save(FOLDERS, folders)
    save(APPEARANCE, appearance)
    print("\nwritten -- reopen Obsidian to see it")
else:
    print("\ndry run -- re-run with --write (with Obsidian closed)")
