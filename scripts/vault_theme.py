"""Keep the vault's three colour systems saying the same thing.

Obsidian colours the same folders in three unrelated places:
  .obsidian/graph.json                             -- graph node groups (rgb as int)
  .obsidian/plugins/color-folders-files/data.json  -- the file explorer
  .obsidian/appearance.json                        -- theme and accent

This script is the single source of truth, so a folder can't be one colour in
the sidebar and another in the graph.

    python scripts/vault_theme.py              dry run, prints what would change
    python scripts/vault_theme.py --preview    writes an HTML swatch sheet
    python scripts/vault_theme.py --write      applies it  (OBSIDIAN CLOSED)

Run --write with OBSIDIAN CLOSED. It writes graph.json out when it exits and
will overwrite anything changed underneath it.

-------------------------------------------------------------------------
The palette, and why it is built rather than picked
-------------------------------------------------------------------------
Rewritten 2026-10-22 after Chuen sent a screenshot of another vault whose
colours he preferred, asking for the same treatment in purple instead of red.
Three things make that one work, and only one of them is the hue:

1. SOLID CHIPS, NOT COLOURED TEXT. The folder name sits on a full-width
   pastel background with dark text. The old version here set `textColor`
   only, which is why it read as ten different-coloured words rather than a
   designed sidebar. `color-folders-files` supports `backgroundColor` --
   checked in its main.js, which assigns element.style.backgroundColor.

2. ONE RAMP, NOT TEN CHOICES. Every chip is the same saturation and the same
   lightness; only the hue moves, by a fixed step, in sidebar order. That is
   what makes it look deliberate. Hand-picking ten hex codes never does.

3. THE GRAPH'S DOMINANT COLOUR IS THE ACCENT, NOT A GROUP. Most nodes match
   no folder rule and fall back to the accent. His reads red because his
   accent is warm. Making this one read purple is therefore a one-line job
   (ACCENT below, already the orb's violet) -- the folder groups are the
   minority colours scattered through it.

So: a narrow arc centred on violet. Wide enough that the sidebar is a visible
gradient top to bottom, narrow enough that every node in the graph is some
shade of blue-violet-pink and the whole thing reads purple.
"""
import colorsys
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
V = r"C:\Users\Xichu\OneDrive - King's College London\Denisse"
WRITE = "--write" in sys.argv
PREVIEW = "--preview" in sys.argv

GRAPH = os.path.join(V, ".obsidian", "graph.json")
FOLDERS = os.path.join(V, ".obsidian", "plugins", "color-folders-files", "data.json")
APPEARANCE = os.path.join(V, ".obsidian", "appearance.json")

ACCENT = "#a855f7"      # the orb's violet. Also what most graph nodes become.
INK = "#1c1130"         # near-black violet for text on a chip -- not pure black
HUE_FROM, HUE_TO = 226, 336   # blue -> pink, with violet landing mid-ramp
SAT, LIGHT = 0.88, 0.76       # candy pastel, not washed out: the reference
                              # vault's chips sit around this, and 0.72/0.78
                              # came out visibly chalkier than the screenshot

# Sidebar order -- Obsidian sorts folders alphabetically, and the ramp follows
# that order so the gradient runs cleanly down the pane. Reordering this list
# reorders the colours, which is almost never what you want.
#
# (path, apply_to_subfolders)
AREAS = [
    ("AKC", True),
    ("Denisse", True),
    ("GitHub", True),
    ("KCLMSoc", True),
    ("Log", True),
    ("Memory", True),
    ("Projects", True),
    ("Robotic Tail", True),
    ("SEM 1 Modules", True),
    ("Templates", True),
]

# Subfolders that earn their own node colour. They stay on the PARENT'S hue
# and move in shade: a hue offset collided (Denisse/Evals landed on exactly
# AKC's blue), and a shade reads as "part of Denisse" where a neighbouring
# hue reads as a separate area.
# (lightness delta, saturation delta). Saturation comes DOWN as lightness
# does, or a darker shade goes vivid instead of deeper -- the first attempt
# used lightness alone and produced #2c32f2, a saturated blue that the dark
# chip text cannot be read on. Every chip has to stay light enough for INK.
SUBS = {
    "Denisse/Brain": (+0.08, -0.14),
    "Denisse/Evals": (-0.06, -0.26),
    "Denisse/Project Log": (-0.12, -0.38),
}

# Explorer-only: the quiet things. A chip would shout, so these stay as text.
MUTED = {
    "Templates": {"textColor": "#7c6f9a", "italic": True},
    "Denisse/Task Archive": {"textColor": "#7c6f9a", "italic": True},
    "Denisse/Vault Health.md": {"textColor": "#6b5f87", "italic": True},
}


def hexof(h, s=SAT, l=LIGHT):
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def ramp():
    """One hue per area, evenly spaced across the arc, in sidebar order."""
    n = len(AREAS)
    step = (HUE_TO - HUE_FROM) / (n - 1)
    return {p: HUE_FROM + i * step for i, (p, _) in enumerate(AREAS)}


HUES = ramp()
LIGHTS = {p: LIGHT for p in HUES}
SATS = {p: SAT for p in HUES}
for sub, (dl, ds) in SUBS.items():
    parent = sub.split("/")[0]
    HUES[sub] = HUES[parent]
    LIGHTS[sub] = LIGHT + dl
    SATS[sub] = SAT + ds


def swatch(p):
    return hexof(HUES[p], s=SATS[p], l=LIGHTS[p])


# Two folders the same colour is the one failure this script exists to stop,
# so it is asserted rather than eyeballed. It has already caught one: a hue
# offset put Denisse/Evals on exactly AKC's blue.
_seen = {}
for _p in HUES:
    _hx = swatch(_p)
    assert _hx not in _seen, f"{_p} and {_seen[_hx]} are both {_hx}"
    _seen[_hx] = _p
# Dark chip text needs a light chip. 0.60 is about where INK stops reading.
for _p in HUES:
    assert LIGHTS[_p] >= 0.60, f"{_p} at lightness {LIGHTS[_p]:.2f} is too dark for INK"


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


def exists(p):
    return os.path.exists(os.path.join(V, p.replace("/", os.sep)))


# -- graph groups -----------------------------------------------------------
# Subfolder rules first: Obsidian takes the first match, so path:"Denisse"
# listed above path:"Denisse/Brain" would swallow it.
order = sorted(HUES, key=lambda p: (p.count("/") == 0, p))
graph = load(GRAPH)
before = {g["query"]: g["color"]["rgb"] for g in graph.get("colorGroups", [])}
graph["colorGroups"] = [
    {"query": f'path:"{p}"', "color": {"a": 1, "rgb": to_int(swatch(p))}}
    for p in order if exists(p)
]
print("graph.json — node groups")
for g in graph["colorGroups"]:
    q, rgb = g["query"], g["color"]["rgb"]
    mark = " " if before.get(q) == rgb else ("~" if q in before else "+")
    print(f"  {mark} {q:<34} #{rgb:06x}")
for q in before:
    if q not in {g['query'] for g in graph['colorGroups']}:
        print(f"  - {q:<34} #{before[q]:06x}  (dropped)")
print("    every other node falls back to the accent — that is the purple bulk")

# -- the file explorer ------------------------------------------------------
folders = load(FOLDERS)
old = dict(folders.get("styles", {}))
styles = {}
for p, subs in AREAS:
    if not exists(p):
        continue
    styles[p] = {"backgroundColor": swatch(p), "textColor": INK,
                 "bold": True, "applyToSubfolders": subs}
for p in SUBS:
    if exists(p):
        styles[p] = {"backgroundColor": swatch(p), "textColor": INK}
for p, e in MUTED.items():
    if exists(p):
        styles[p] = e          # deliberately no chip
folders["styles"] = styles

print("\ncolor-folders-files — the sidebar")
for p, e in styles.items():
    was = old.get(p)
    mark = " " if was == e else ("~" if was else "+")
    shown = e.get("backgroundColor") or e.get("textColor")
    kind = "chip" if "backgroundColor" in e else "text"
    print(f"  {mark} {p:<34} {shown}  {kind}")
for p in old:
    if p not in styles:
        print(f"  - {p:<34} (dropped)")

# -- accent -----------------------------------------------------------------
appearance = load(APPEARANCE)
print("\nappearance.json")
print(f"  accentColor {appearance.get('accentColor') or '(default)'} -> {ACCENT}")
appearance["accentColor"] = ACCENT

# -- preview ----------------------------------------------------------------
if PREVIEW:
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "logs", "palette.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    chips = "".join(
        f'<div class="row"><span class="chip" style="background:{e.get("backgroundColor","transparent")};'
        f'color:{e.get("textColor", INK)};'
        f'{"font-style:italic;" if e.get("italic") else ""}">{p}</span>'
        f'<code>{e.get("backgroundColor") or e.get("textColor")}</code></div>'
        for p, e in styles.items())
    dots = "".join(
        f'<span class="dot" style="background:{swatch(p)}" title="{p}"></span>'
        for p in order if exists(p))
    html = f"""<!doctype html><meta charset="utf-8"><title>Vault palette</title>
<style>
 body{{background:#15101f;color:#cdc4e0;font:14px/1.5 ui-sans-serif,system-ui;
      margin:0;padding:40px;display:flex;gap:56px;flex-wrap:wrap}}
 h2{{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:#8b7fa8;
     font-weight:600;margin:0 0 18px}}
 .pane{{min-width:300px}}
 .row{{display:flex;align-items:center;gap:12px;margin:5px 0}}
 .chip{{display:block;width:230px;padding:5px 11px;border-radius:7px;
        font-weight:600;font-size:13px}}
 code{{color:#6b5f87;font-size:11px}}
 .graph{{background:#0d0912;border-radius:14px;padding:30px;width:330px;
         display:flex;flex-wrap:wrap;gap:11px;align-content:flex-start}}
 .dot{{width:16px;height:16px;border-radius:50%;display:block}}
 .accent{{width:26px;height:26px;border-radius:50%;background:{ACCENT};display:block}}
 p{{color:#8b7fa8;font-size:12px;max-width:330px}}
</style>
<div class="pane"><h2>Sidebar</h2>{chips}</div>
<div class="pane"><h2>Graph</h2><div class="graph">
  <span class="accent"></span><span class="accent"></span><span class="accent"></span>
  <span class="accent"></span><span class="accent"></span><span class="accent"></span>
  {dots}</div>
<p>The big dots are the accent — most nodes match no folder rule and land
there, which is why the graph reads purple overall. The small dots are the
folder groups scattered through it.</p></div>
"""
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print(f"\npreview -> {out}")

if WRITE:
    save(GRAPH, graph)
    save(FOLDERS, folders)
    save(APPEARANCE, appearance)
    print("\nwritten — reopen Obsidian to see it")
elif not PREVIEW:
    print("\ndry run — re-run with --write (with Obsidian closed)")
