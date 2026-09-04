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

from PIL import Image, ImageDraw, ImageFilter, ImageTk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POSITION_FILE = os.path.join(BASE_DIR, "memory", "orb_position.json")

ACCENT = (168, 85, 247)
ACCENT_BRIGHT = (201, 163, 255)
ACCENT_DEEP = (76, 29, 149)

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
    """A glossy sphere with an offset highlight, at 4x scale for
    anti-aliasing. `breath` in [0, 1] sets how 'inhaled' (bigger/brighter)
    this frame is -- alternating two of these is the pulse animation."""
    scale = 4
    px = size_px * scale
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r = px / 2 * (0.86 + 0.14 * breath)
    cx = cy = px / 2
    steps = max(1, int(r))
    for i in range(steps, 0, -1):
        t = i / steps
        col = tuple(round(ACCENT_BRIGHT[c] * (1 - t) + ACCENT_DEEP[c] * t) for c in range(3))
        draw.ellipse((cx - i, cy - i, cx + i, cy + i), fill=(*col, 255))
    highlight = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    hr = r * 0.42
    hd.ellipse((cx - r * 0.62 - hr, cy - r * 0.68 - hr, cx - r * 0.62 + hr, cy - r * 0.68 + hr), fill=(255, 255, 255, 130))
    highlight = highlight.filter(ImageFilter.GaussianBlur(px * 0.05))
    img = Image.alpha_composite(img, highlight)
    return img.resize((size_px, size_px), Image.LANCZOS)


def _make_orb_frame(total_px: int, orb_px: int, breath: float) -> ImageTk.PhotoImage:
    """The full frame shown on the canvas: soft outer glow + glossy core,
    centered in a transparent total_px x total_px square."""
    scale = 4
    size = total_px * scale
    frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    core = _make_orb_core(orb_px * scale, breath)

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gr = core.size[0] * 0.6
    gd.ellipse((size / 2 - gr, size / 2 - gr, size / 2 + gr, size / 2 + gr), fill=(*ACCENT, round(150 + 60 * breath)))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.05))
    frame = Image.alpha_composite(frame, glow)

    cd = core.size[0]
    frame.alpha_composite(core, (round((size - cd) / 2), round((size - cd) / 2)))
    frame = frame.resize((total_px, total_px), Image.LANCZOS)
    return ImageTk.PhotoImage(frame)


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
        self.win.attributes("-transparentcolor", TRANSPARENT_KEY)
        self.win.config(bg=TRANSPARENT_KEY)

        self.canvas = tk.Canvas(
            self.win, width=self.total_px, height=self.total_px,
            bg=TRANSPARENT_KEY, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        # Two alternating frames (resting / "inhaled") for a slow breathing
        # pulse -- cheap sprite-swap animation, no per-tick PIL work.
        self._frames = [
            _make_orb_frame(self.total_px, self.orb_px, _breath_curve(i, BREATH_FRAMES))
            for i in range(BREATH_FRAMES)
        ]
        self._frame_i = 0
        c = self.total_px / 2
        self._orb_img_id = self.canvas.create_image(c, c, image=self._frames[0])

        # JARVIS-style targeting ring -- two native Canvas arcs (cheap to
        # animate: just nudging their `start` angle each tick, no image
        # regeneration needed) rather than a PIL image like the orb.
        ring_r = self.orb_px / 2 + round(RING_GAP * scale)
        self._ring_main = self.canvas.create_arc(
            c - ring_r, c - ring_r, c + ring_r, c + ring_r,
            start=0, extent=150, style="arc", outline="#c9a3ff", width=max(1, round(1.5 * scale)),
        )
        self._ring_echo = self.canvas.create_arc(
            c - ring_r, c - ring_r, c + ring_r, c + ring_r,
            start=180, extent=60, style="arc", outline="#7c3aed", width=max(1, round(1.5 * scale)),
        )
        self._ring_angle = 0

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
            self._frame_i = (self._frame_i + 1) % len(self._frames)
            self.canvas.itemconfig(self._orb_img_id, image=self._frames[self._frame_i])
            self._ring_angle = (self._ring_angle + RING_DEG_PER_TICK) % 360
            self.canvas.itemconfig(self._ring_main, start=self._ring_angle)
            self.canvas.itemconfig(self._ring_echo, start=(self._ring_angle + 180) % 360)
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
