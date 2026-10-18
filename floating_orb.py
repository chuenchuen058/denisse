"""
Denisse -- floating desktop orb.

A small, always-on-top, draggable circular widget (a Siri-style glowing
orb with a JARVIS-style rotating targeting ring) that sits on the desktop
outside the tray icon and chat popup -- a persistent, always-visible way
in. Click it without dragging to open the chat popup (wake-word listening
stays a separate, manual opt-in via the tray menu -- it costs real CPU
continuously once on, so the orb doesn't turn it on as a side effect of
just opening the chat); drag it anywhere and it remembers where you left
it (see POSITION_FILE) across restarts.

Built with plain Canvas + PIL images, same techniques as tray.py's chat
bubbles/send button -- kept in its own module since it's a self-contained
widget tray.py just instantiates and points at a callback, not something
any of tray.py's chat logic needs to know the internals of.
"""

import json
import math
import os
import time
import tkinter as tk

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POSITION_FILE = os.path.join(BASE_DIR, "memory", "orb_position.json")

ACCENT = (168, 85, 247)
ACCENT_BRIGHT = (201, 163, 255)
ACCENT_DEEP = (76, 29, 149)
ACCENT_HEX = "#c9a3ff"  # ACCENT_BRIGHT as a Tk color string, for plain (non-PIL) Canvas items

# An arbitrary color that's vanishingly unlikely to appear in the orb's own
# art -- Tk's -transparentcolor makes pixels this exact color both
# invisible AND click-through on Windows, which is what makes this read as
# a round floating widget instead of a square window with a circle in it.
TRANSPARENT_KEY = "#010203"

ORB_SIZE = 34  # logical px, diameter of the orb itself
GLOW_PAD = 12  # extra room around it for the soft outer glow to bleed into
RING_GAP = 5  # gap between the orb's edge and the rotating ring

TICK_MS = 50  # animation timer interval -- 20Hz, smooth without being wasteful
BREATH_FRAMES = 24  # precomputed frames for one full pulse cycle (24 * 50ms = 1.2s/breath)
RING_DEG_PER_TICK = 3  # 360 / 3 * 50ms = 6s per full rotation
RING_FRAMES = 360 // RING_DEG_PER_TICK  # precomputed frames for one full ring rotation

SUPERSAMPLE = 4  # render the orb core/glow at this multiple then
# LANCZOS-downsize, same as it always was -- the core's concentric-circle
# gradient fill already applies this twice (once for its own pixel size,
# once again inside _make_orb_core), so pushing this higher blows up
# startup time fast (measured: 6x here -> 8+s to precompute 24 frames,
# vs a fraction of a second at 4x) for a part that wasn't the complaint.
RING_SUPERSAMPLE = 6  # the ring is just two arc strokes (no per-pixel
# gradient loop), so it can afford a higher supersample cheaply -- this is
# the part that was actually visibly jagged (raw, non-anti-aliased Canvas
# arcs), so it gets the bigger quality bump.

# Windows' -transparentcolor is a colorkey, not real per-pixel alpha: only a
# pixel that's an EXACT match for TRANSPARENT_KEY is dropped/click-through.
# A softly blurred glow's edge fades through a long tail of low-but-nonzero
# alpha, and every one of those pixels blends to a faint solid smudge
# against the key instead of vanishing -- that smudge is what read as
# visible "gaps"/fringing around the orb and ring. Snapping anything below
# this threshold straight to fully transparent finishes the fade cleanly.
ALPHA_SNAP_THRESHOLD = 14


def _snap_low_alpha(img: "Image.Image") -> "Image.Image":
    r, g, b, a = img.split()
    a = a.point(lambda v: 0 if v < ALPHA_SNAP_THRESHOLD else v)
    return Image.merge("RGBA", (r, g, b, a))


# Snapping the faint tail wasn't enough: Tk still blends every remaining
# part-alpha pixel onto the canvas bg, TRANSPARENT_KEY -- near-black -- so
# the glow and every anti-aliased rim came out as a ragged dark halo, worst
# on white windows (asked 2026-10-14: "glitchy look and odd shapes").
# _flatten decides each pixel itself instead: opaque enough -> solid,
# blended onto a purple matte so edges stay purple whatever is behind;
# otherwise -> exactly the key, i.e. gone.
MATTE = ACCENT
FLATTEN_ALPHA = 110


def _flatten(img: "Image.Image", matte=MATTE, cut=FLATTEN_ALPHA) -> "Image.Image":
    """RGBA with all-or-nothing alpha, for a -transparentcolor window.
    Still RGBA (not RGB) so the ring layer can sit over the orb layer."""
    solid = Image.new("RGBA", img.size, (*matte, 255))
    solid.alpha_composite(img)
    solid.putalpha(img.getchannel("A").point(lambda v: 255 if v >= cut else 0))
    return solid


def _breath_curve(i: int, n: int) -> float:
    """Smooth 0->1->0 breathing value for frame i of n, via a sine wave --
    replaces the old 2-frame on/off toggle, which read as a flicker rather
    than a pulse."""
    return (math.sin(2 * math.pi * i / n - math.pi / 2) + 1) / 2


def _dpi_scale(root: tk.Misc) -> float:
    try:
        return root.winfo_fpixels("1i") / 96.0
    except Exception:
        return 1.0


def _make_orb_core(size_px: int, breath: float) -> Image.Image:
    """A glossy sphere with an offset highlight, at SUPERSAMPLE scale for
    anti-aliasing. `breath` in [0, 1] sets how 'inhaled' (bigger/brighter)
    this frame is -- alternating two of these is the pulse animation."""
    scale = SUPERSAMPLE
    px = size_px * scale
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Pulse amplitude is 32% stronger than the original 0.86-1.0 swing --
    # two bumps requested 2026-09-04 (20%, then another 10% on top:
    # 1.2*1.1=1.32). Peak stays at 1.0 (fills the core canvas exactly) to
    # avoid clipping, so the extra swing comes from a deeper dip at rest
    # instead: 1.0 - 1.32*0.14 = 0.8152.
    r = px / 2 * (0.8152 + 0.1848 * breath)
    cx = cy = px / 2
    steps = max(1, int(r))
    for i in range(steps, 0, -1):
        t = i / steps
        col = tuple(round(ACCENT_BRIGHT[c] * (1 - t) + ACCENT_DEEP[c] * t) for c in range(3))
        draw.ellipse((cx - i, cy - i, cx + i, cy + i), fill=(*col, 255))
    highlight = Image.new("RGBA", (px, px), (255, 255, 255, 0))
    hd = ImageDraw.Draw(highlight)
    hr = r * 0.42
    hd.ellipse((cx - r * 0.62 - hr, cy - r * 0.68 - hr, cx - r * 0.62 + hr, cy - r * 0.68 + hr), fill=(255, 255, 255, 130))
    highlight = highlight.filter(ImageFilter.GaussianBlur(px * 0.05))
    # Clip the shine to the sphere -- its blur spilled past the edge and,
    # once flattened, stuck out as a lavender bump (2026-10-14).
    highlight.putalpha(ImageChops.multiply(highlight.getchannel("A"), img.getchannel("A")))
    img = Image.alpha_composite(img, highlight)
    return img.resize((size_px, size_px), Image.LANCZOS)


def _make_orb_frame(total_px: int, orb_px: int, breath: float) -> Image.Image:
    """The full frame as RGBA: soft outer glow + glossy core, centered in
    a transparent total_px x total_px square. Real alpha -- the orb window
    shows it with layered.py, so the glow stays soft on any background."""
    scale = SUPERSAMPLE
    size = total_px * scale
    frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    core = _make_orb_core(orb_px * scale, breath)

    # Transparent pixels carry the glow's own colour, not black -- the blur
    # mixes their RGB in, and black made the glow's edge go grey on white.
    glow = Image.new("RGBA", (size, size), (*ACCENT, 0))
    gd = ImageDraw.Draw(glow)
    gr = core.size[0] * 0.6
    # Same 32% amplitude bump as the core radius above (60 -> 79.2), floor
    # unchanged so the glow at rest still looks the same as before.
    gd.ellipse((size / 2 - gr, size / 2 - gr, size / 2 + gr, size / 2 + gr), fill=(*ACCENT, round(150 + 79.2 * breath)))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.05))
    frame = Image.alpha_composite(frame, glow)

    cd = core.size[0]
    frame.alpha_composite(core, (round((size - cd) / 2), round((size - cd) / 2)))
    return frame.resize((total_px, total_px), Image.LANCZOS)


def _make_ring_frame(total_px: int, ring_r: float, angle: float) -> Image.Image:
    """One frame of the JARVIS-style targeting ring, rendered the same way
    as the orb core (supersampled + LANCZOS-downsized) instead of raw
    Canvas arcs -- native Tk arcs aren't anti-aliased on Windows, which is
    what made the rotating ring look jagged/gappy rather than a clean
    stroke. `angle` is the rotation offset in degrees for this frame."""
    scale = RING_SUPERSAMPLE
    size = total_px * scale
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r = ring_r * scale
    c = size / 2
    bbox = (c - r, c - r, c + r, c + r)
    # A thin (~1.5px) anti-aliased stroke at this widget's tiny final size
    # (~58px across) has almost no fully-opaque "core" -- nearly every pixel
    # along the curve ends up a partial-coverage edge pixel, so downsizing
    # left the whole ring faint and, where visible, blotchy rather than a
    # clean line (measured: mean alpha ~81/255 at 1.5px vs ~127/255 here).
    # A wider stroke gives the curve room for a solid center under the AA.
    width = max(1, round(3.5 * scale))
    draw.arc(bbox, start=angle, end=angle + 150, fill=(*ACCENT_BRIGHT, 255), width=width)
    draw.arc(bbox, start=angle + 180, end=angle + 240, fill=(124, 58, 237, 255), width=width)
    return img.resize((total_px, total_px), Image.LANCZOS)


class FloatingOrb:
    def __init__(self, root: tk.Tk, on_activate):
        self.root = root
        self.on_activate = on_activate
        scale = _dpi_scale(root)

        self.orb_px = round(ORB_SIZE * scale)
        self.total_px = round((ORB_SIZE + GLOW_PAD * 2) * scale)

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.config(bg=TRANSPARENT_KEY)

        # Still here for the mouse (click, drag, right-click); with the
        # layered painter below it's never what's seen.
        self.canvas = tk.Canvas(
            self.win, width=self.total_px, height=self.total_px,
            bg=TRANSPARENT_KEY, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        # Breathing pulse (BREATH_FRAMES) and JARVIS-style targeting ring
        # (RING_FRAMES), both supersampled PIL frames -- Tk's own arcs
        # aren't anti-aliased on Windows, which is what made the ring look
        # jagged/gappy. Both advance one frame a tick, so the combined
        # loop is RING_FRAMES long (a multiple of BREATH_FRAMES); each
        # combined frame is pre-flattened once, no per-tick PIL work.
        ring_r = self.orb_px / 2 + round(RING_GAP * scale)
        orbs = [_make_orb_frame(self.total_px, self.orb_px, _breath_curve(i, BREATH_FRAMES)) for i in range(BREATH_FRAMES)]
        rings = [_make_ring_frame(self.total_px, ring_r, i * RING_DEG_PER_TICK) for i in range(RING_FRAMES)]
        self._frame_i = 0  # breath index -- reminder_card reads it to breathe in step
        self._tick_i = 0
        self._painter = None
        try:
            from layered import LayeredPainter, to_bgra
            self._painter = LayeredPainter(self.win, (self.total_px, self.total_px))
            self._combined = [to_bgra(Image.alpha_composite(orbs[i % BREATH_FRAMES], rings[i])) for i in range(RING_FRAMES)]
            if not self._painter.show(self._combined[0]):
                raise OSError("UpdateLayeredWindow failed")
        except Exception:
            # Fallback: the old colour-keyed window, crisp edges, no soft glow.
            if self._painter is not None:
                self._painter.close()
            self._painter = None
            self.win.attributes("-transparentcolor", TRANSPARENT_KEY)
            self._frames = [ImageTk.PhotoImage(_flatten(o)) for o in orbs]
            self._ring_frames = [ImageTk.PhotoImage(_flatten(r)) for r in rings]
            c = self.total_px / 2
            self._orb_img_id = self.canvas.create_image(c, c, image=self._frames[0])
            self._ring_img_id = self.canvas.create_image(c, c, image=self._ring_frames[0])

        # Speech bubble -- a separate small Toplevel (a widget can't be
        # placed outside its own parent window's bounds in Tk), shown just
        # above wherever the orb currently sits, for the "says something
        # like a Siri chat" touch.
        self._bubble = tk.Toplevel(root)
        self._bubble.overrideredirect(True)
        self._bubble.attributes("-topmost", True)
        self._bubble.withdraw()
        self._bubble_label = tk.Label(
            self._bubble, text="", bg="#0d0916", fg="#ffffff", font=("Segoe UI", 9),
            padx=10, pady=6, wraplength=round(220 * scale), justify="left",
            highlightbackground="#a855f7", highlightthickness=1,
        )
        self._bubble_label.pack()
        self._bubble_hide_job = None

        self._dragging = False
        self._drag_start = (0, 0)
        self._win_start = (0, 0)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.config(cursor="hand2")

        self._place_initial(scale)
        self._animate()

    # -- placement / persistence --

    def _place_initial(self, scale: float):
        pos = self._load_position()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        if pos is not None:
            x, y = pos
        else:
            x = sw // 2 - self.total_px // 2
            y = sh - self.total_px - round(72 * scale)  # default: bottom-center, clear of the taskbar
        x = max(0, min(x, sw - self.total_px))
        y = max(0, min(y, sh - self.total_px))
        self.win.geometry(f"{self.total_px}x{self.total_px}+{x}+{y}")

    def _load_position(self):
        try:
            with open(POSITION_FILE) as f:
                data = json.load(f)
            return data["x"], data["y"]
        except Exception:
            return None

    def _save_position(self):
        try:
            os.makedirs(os.path.dirname(POSITION_FILE), exist_ok=True)
            data = {"x": self.win.winfo_x(), "y": self.win.winfo_y()}
            with open(POSITION_FILE, "w") as f:
                json.dump(data, f)
        except Exception:
            pass  # position memory is a nicety, never worth crashing over

    # -- drag vs. click --

    def _on_press(self, event):
        self._dragging = False
        self._drag_start = (event.x_root, event.y_root)
        self._win_start = (self.win.winfo_x(), self.win.winfo_y())
        self._last_move_t = 0.0

    def _on_motion(self, event):
        # Windows can fire dozens of these per second; moving a layered
        # (-transparentcolor) window is real DWM recompositing work each
        # time, not a cheap no-op, so cap it to ~60Hz instead of matching
        # every single mouse-move event -- that's what read as "laggy".
        now = time.monotonic()
        if now - self._last_move_t < 0.016:
            return
        self._last_move_t = now

        dx = event.x_root - self._drag_start[0]
        dy = event.y_root - self._drag_start[1]
        if abs(dx) > 4 or abs(dy) > 4:
            self._dragging = True
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        nx = max(0, min(self._win_start[0] + dx, sw - self.total_px))
        ny = max(0, min(self._win_start[1] + dy, sh - self.total_px))
        self.win.geometry(f"+{nx}+{ny}")

    def _on_release(self, event):
        if self._dragging:
            self._save_position()
        else:
            self.on_activate()

    # -- animation --

    def _animate(self):
        try:
            self._tick_i = (self._tick_i + 1) % RING_FRAMES
            self._frame_i = self._tick_i % BREATH_FRAMES
            if self._painter is not None:
                if not self.win.winfo_exists():
                    self._painter.close()
                    return
                self._painter.show(self._combined[self._tick_i])
            else:
                self.canvas.itemconfig(self._orb_img_id, image=self._frames[self._frame_i])
                self.canvas.itemconfig(self._ring_img_id, image=self._ring_frames[self._tick_i])
        except tk.TclError:
            return  # window torn down (app quitting) -- just stop
        self.root.after(TICK_MS, self._animate)

    # -- speech bubble --

    def say(self, text: str, duration_ms: int = 4000):
        """Show a brief speech-bubble above the orb's current position --
        used for live 'here's what I heard' feedback, same data as the
        chat popup's own show_heard(), not a new recording of anything."""
        try:
            if self._bubble_hide_job is not None:
                self.root.after_cancel(self._bubble_hide_job)
                self._bubble_hide_job = None
            self._bubble_label.config(text=text)
            self._bubble.update_idletasks()
            bw, bh = self._bubble.winfo_reqwidth(), self._bubble.winfo_reqheight()
            ox, oy = self.win.winfo_x(), self.win.winfo_y()
            x = ox + self.total_px // 2 - bw // 2
            y = oy - bh - 8
            sw = self.win.winfo_screenwidth()
            x = max(4, min(x, sw - bw - 4))
            self._bubble.geometry(f"{bw}x{bh}+{x}+{max(0, y)}")
            self._bubble.deiconify()
            self._bubble_hide_job = self.root.after(duration_ms, self._hide_bubble)
        except tk.TclError:
            pass

    def _hide_bubble(self):
        self._bubble_hide_job = None
        try:
            self._bubble.withdraw()
        except tk.TclError:
            pass
