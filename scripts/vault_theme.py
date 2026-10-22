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

So: a full-wheel ramp built in OKLab, starting at violet. Every chip the same
perceived lightness and chroma, 36 degrees of hue apart -- as separable as ten
colours can be while still looking like one set. The graph reads purple
regardless, because that comes from ACCENT.
"""
import io
import json
import math
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

# A FULL wheel, starting at violet. The first version used a narrow 226-336
# arc to keep everything purple, which put neighbouring folders ~12 deg apart
# -- AKC, Denisse and GitHub came out as three near-identical blues and Chuen
# said he could not tell them apart. He was right, and the narrow arc was
# never buying anything: the graph reads purple because unmatched nodes fall
# back to ACCENT, not because of these. So the sidebar gets the whole
# spectrum for maximum separation and the graph stays purple regardless.
# NOTE these are OKLCH hue angles, which are NOT the HSL ones: 270 here is a
# periwinkle blue, not violet. The ramp lands Denisse (306) on lavender, which
# happens to be the orb's own colour, and SEM 1 Modules on cyan. Shift
# HUE_FROM to rotate which folder gets which; the spacing stays even.
HUE_FROM, HUE_SPAN = 270, 360   # blue -> violet -> pink -> warm -> green -> back

# Colours are built in OKLab, not HSL, and this is the part that actually
# matters. Two earlier attempts failed on the same thing -- HSL's "lightness"
# and "saturation" mean something different at every hue:
#   * equal HSL lightness  -> yellows blinding, blues muddy;
#   * equal WCAG luminance -> greens driven to #13e428, electric, not pastel.
# OKLab is built so that equal L and equal C LOOK equal at any hue, which is
# exactly the property a ten-hue pastel ramp needs. L and C below are read off
# the kind of chip Chuen liked in the reference vault.
OK_L, OK_C = 0.845, 0.105        # lightness, chroma
SUB_L, SUB_C = 0.845, 0.105      # subfolders override these per entry

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
# (luminance delta, saturation delta), both relative to the parent. Dropping
# saturation alongside luminance keeps a darker shade deeper rather than more
# vivid -- an early version moved lightness alone and produced #2c32f2, a
# saturated blue the dark chip text could not be read on.
SUBS = {              # (OKLab lightness delta, chroma delta)
    "Denisse/Brain": (+0.07, -0.045),
    "Denisse/Evals": (-0.07, +0.015),
    "Denisse/Project Log": (-0.14, +0.005),
}

# Explorer-only: the quiet things. A chip would shout, so these stay as text.
MUTED = {
    "Templates": {"textColor": "#7c6f9a", "italic": True},
    "Denisse/Task Archive": {"textColor": "#7c6f9a", "italic": True},
    "Denisse/Vault Health.md": {"textColor": "#6b5f87", "italic": True},
}


def _gamma(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def oklch_rgb(L, C, h_deg):
    """OKLCH -> linear sRGB. Returns floats, which may fall outside 0..1."""
    a = C * math.cos(math.radians(h_deg))
    b = C * math.sin(math.radians(h_deg))
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (+4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
            -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
            -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)


def hexof(h, L=None, C=None):
    """A hex for this hue at the ramp's lightness and chroma.

    Not every hue can hold the same chroma inside sRGB -- the gamut is
    lopsided, and a vivid yellow-green exists where an equally vivid blue does
    not. So chroma is reduced until the colour fits. Clamping the channels
    instead would shift the hue, which is exactly the thing this is for.
    """
    L = OK_L if L is None else L
    C = OK_C if C is None else C
    for _ in range(60):
        rgb = oklch_rgb(L, C, h)
        if all(-1e-4 <= v <= 1 + 1e-4 for v in rgb):
            break
        C *= 0.97
    r, g, b = (min(1.0, max(0.0, _gamma(v))) for v in rgb)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def luminance_of(hx):
    r, g, b = [int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ramp():
    """One hue per area, evenly spaced round the wheel, in sidebar order."""
    n = len(AREAS)
    step = HUE_SPAN / n          # /n not /(n-1): the wheel wraps, so the last
    return {p: HUE_FROM + i * step  # folder must not land back on the first
            for i, (p, _) in enumerate(AREAS)}


HUES = ramp()
OKL = {p: OK_L for p in HUES}
OKC = {p: OK_C for p in HUES}
for sub, (dL, dC) in SUBS.items():
    HUES[sub] = HUES[sub.split("/")[0]]
    OKL[sub] = OK_L + dL
    OKC[sub] = OK_C + dC


def swatch(p):
    return hexof(HUES[p], OKL[p], OKC[p])


# Two folders the same colour is the one failure this script exists to stop,
# so it is asserted rather than eyeballed. It has already caught one: a hue
# offset put Denisse/Evals on exactly AKC's blue.
_seen = {}
for _p in HUES:
    _hx = swatch(_p)
    assert _hx not in _seen, f"{_p} and {_seen[_hx]} are both {_hx}"
    _seen[_hx] = _p
# The dark ink has to be readable on every chip, so check the real thing --
# WCAG contrast ratio -- rather than a lightness number that means something
# different on a yellow than on a blue. 4.5 is the AA threshold for body text.
_INK_LUM = luminance_of(INK)
for _p in HUES:
    _l = luminance_of(swatch(_p))
    _ratio = (max(_l, _INK_LUM) + 0.05) / (min(_l, _INK_LUM) + 0.05)
    assert _ratio >= 4.5, f"{_p} {swatch(_p)}: ink contrast only {_ratio:.1f}:1"


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
print("graph.json â€” node groups")
for g in graph["colorGroups"]:
    q, rgb = g["query"], g["color"]["rgb"]
    mark = " " if before.get(q) == rgb else ("~" if q in before else "+")
    print(f"  {mark} {q:<34} #{rgb:06x}")
for q in before:
    if q not in {g['query'] for g in graph['colorGroups']}:
        print(f"  - {q:<34} #{before[q]:06x}  (dropped)")
print("    every other node falls back to the accent â€” that is the purple bulk")

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

print("\ncolor-folders-files â€” the sidebar")
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
<p>The big dots are the accent â€” most nodes match no folder rule and land
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
    print("\nwritten â€” reopen Obsidian to see it")
elif not PREVIEW:
    print("\ndry run â€” re-run with --write (with Obsidian closed)")
