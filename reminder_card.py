"""
Denisse -- reminder card: the orb messaging him, WhatsApp-style.

Shows the open task list (tasks.py) as an incoming chat message from the
orb: a bubble beside it, vertically centred on the orb, with a tail out of
the middle of its side pointing at it, a "typing" bubble first, and a
timestamp in the corner. It follows the orb when he drags it. Chuen asked
on 2026-10-07 for it to read like the orb talking to him, WhatsApp-style,
and never over the taskbar -- so the bubble is clamped to the screen's
WORK AREA (the part Windows leaves free of the taskbar), and the tail
slides along the side to keep pointing at the orb when the clamp pushes
the bubble off-centre. With no orb it falls back to the bottom-right
corner. Stays up until dismissed (the "Dismiss" link, or
Esc) -- it's the thing he's meant to see first after logging in, so it
doesn't time out like the orb's short speech bubble does.

Every row is editable in place: tick it done, edit the text or deadline,
or delete it, plus an "Add reminder" row at the bottom. All of that goes
straight through tasks.py, so the JSON and the Obsidian mirror stay in step
exactly as they do when the model edits tasks.

Deliberately no model call anywhere in here -- it has to work the second
the machine boots, before Ollama has even finished loading, and a list
read straight off disk can't hallucinate a deadline.
"""

import tkinter as tk
from datetime import datetime, timedelta

import ctypes
from ctypes import wintypes

import math
import time

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageTk

from floating_orb import (
    ACCENT as ORB_GLOW, ACCENT_BRIGHT as ORB_LAVENDER, ACCENT_DEEP as ORB_DEEP,
    BREATH_FRAMES, RING_DEG_PER_TICK, TICK_MS, _breath_curve, _flatten,
)

ORB_RING_DEG_PER_S = RING_DEG_PER_TICK * 1000 / TICK_MS  # 60 deg/s, a turn per 6s
SWEEP_SPEED = 0.4  # the bubble's arcs at 40% of the orb ring's speed -- "60% slower"

ORB_VIOLET = (124, 58, 237)  # the orb ring's second, shorter arc (floating_orb._make_ring_frame)

# Same colorkey trick as floating_orb.py: pixels exactly this color are
# invisible AND click-through, which is what lets the bubble be a rounded
# shape with a tail instead of a rectangle.
TRANSPARENT_KEY = "#010203"


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


# Black and purple, the purple the orb's own and alive like it (asked
# 2026-10-07): a black body whose border is drawn the way the orb is -- a
# breathing glow halo in the orb's glow colour, and the orb's two-tone ring
# (a long lavender arc and a short violet one) sweeping round the edge over
# a deep-violet base line. Breath and sweep are read from the orb's own
# frame counters, so the two pulse and turn together. Every colour comes
# from floating_orb.py so they can't drift apart. A single flat #a855f7
# outline was tried first and didn't read as the orb's tone -- that colour
# only ever appears on the orb as a blurred halo, never flat.
FILL = "#0a0a0c"  # black -- but not TRANSPARENT_KEY, or it would vanish
DIVIDER = "#2a1748"  # the orb's deep violet, dimmed
TEXT = "#ffffff"
MUTED = "#b5a8c9"
FAINT = "#857a99"  # the Dismiss link at rest -- present but clearly secondary
ACCENT = _hex(ORB_LAVENDER)  # #c9a3ff, the orb ring's lavender
BAD = "#ff6b70"
WARN = "#ffcb52"
GOOD = "#3ddc84"
ENTRY_BG = "#17121f"

CARD_W = 300  # logical px, body width
PAD = 12
RADIUS = 14
TYPING_W = 64  # logical px, the "typing" bubble before the message lands
TYPING_MS = 900
TAIL_LEN = 11  # how far the tail sticks out towards the orb
TAIL_BASE = 16  # how tall the tail is where it meets the bubble
MARGIN = 12  # gap from the work-area edges
SLIDE = 24  # how far it travels out of the orb while sliding in
FOLLOW_MS = 30  # how often to check whether the orb has been dragged
ANIM_MS, ANIM_STEPS = 220, 14
STROKE = 2.2  # thick enough for the ring's two colours to read
GLOW = 9  # logical px of halo around the bubble -- under MARGIN, so it stays off the taskbar
ALIVE_MS = 100  # border redraw interval: every other orb tick, half the cost
LONG_ARC, SHORT_ARC = 150, 60  # degrees, same split as the orb's ring
BREATH_LEVELS = 12  # pre-rendered halo strengths
SUPERSAMPLE = 4
ALPHA_SNAP = 14
MAX_ROWS = 100  # the list scrolls now, so this is just a sanity cap
# The task list sits in a scrolling viewport no taller than this share of
# the work area. Asked 2026-10-08: the card was too big and couldn't
# scroll; 0.23 makes the whole card about half the height it reached
# with 11 tasks (~4 rows visible on this laptop's 1504px work area).
LIST_MAX_FRAC = 0.23
BAR_W = 4  # logical px, the slim scroll thumb

# Priority colours, asked for 2026-10-08: red for the heavily weighted
# marked work, amber for smaller marked pieces and AKC, a cool blue for
# anything optional. The list runs soonest-first across all priorities
# (asked 2026-10-08, after a grouped-by-priority version put a quiz due
# in 11 days under seven items months away); each row's stripe and tick
# carry its priority, with a legend above. The deadline line keeps its
# own red/amber for overdue and due-within-48h.
PRIORITY_ORDER = ("high", "medium", "low")
PRIORITY_COLOR = {"high": "#ff6b70", "medium": "#ffcb52", "low": "#7fb3ff"}
PRIORITY_LABEL = {"high": "High", "medium": "Medium", "low": "Low"}


def _prio(t):
    p = (t.get("priority") or "medium").lower()
    return p if p in PRIORITY_COLOR else "medium"


def _dpi_scale(root):
    try:
        return root.winfo_fpixels("1i") / 96.0
    except Exception:
        return 1.0


class _BubbleArt:
    """The bubble's artwork, split into greyscale masks once per shape so
    each animation frame is only a handful of PIL composites (all C code --
    no numpy in the venv, and per-pixel Python would stall the Tk thread).

    w/h are the BODY size in real px, tail_y where the tail's point sits
    (down from the top), g the halo margin added on every side. The shape
    is drawn tail-on-the-right at SUPERSAMPLE scale, shrunk, and mirrored
    for "left" (a horizontal flip leaves tail_y where it was).
    """

    def __init__(self, w, h, side, tail_y, s, g):
        k = SUPERSAMPLE
        r, tl, tb = round(RADIUS * s) * k, round(TAIL_LEN * s) * k, round(TAIL_BASE * s) * k
        st = max(1.0, STROKE * s) * k
        bw, H = w * k, h * k
        W = bw + tl
        ty = tail_y * k
        base = bw - 1 - st * 3

        def shape(inset):
            m = Image.new("L", (W, H), 0)
            d = ImageDraw.Draw(m)
            d.rounded_rectangle((inset, inset, bw - 1 - inset, H - 1 - inset), radius=max(1, r - inset), fill=255)
            if inset == 0:
                d.polygon([(base, ty - tb / 2), (base, ty + tb / 2), (W - 1, ty)], fill=255)
            else:  # pulled in so the border keeps its thickness to the point
                d.polygon([
                    (base - inset, ty - tb / 2 + inset * 1.3),
                    (base - inset, ty + tb / 2 - inset * 1.3),
                    (W - 1 - inset * 2.4, ty),
                ], fill=255)
            m = m.resize((W // k, h), Image.LANCZOS)
            return ImageOps.mirror(m) if side == "left" else m

        outer, inner = shape(0), shape(st)
        self.size = (W // k + 2 * g, h + 2 * g)
        self.fill = Image.new("L", self.size, 0)
        self.fill.paste(outer, (g, g))
        inner_full = Image.new("L", self.size, 0)
        inner_full.paste(inner, (g, g))
        self.ring = ImageChops.subtract(self.fill, inner_full)
        self.glow = self.fill.filter(ImageFilter.GaussianBlur(g * 0.55))

        # Angle of each border pixel around the body's centre, 0-255 for
        # 0-360 degrees, measured the way PIL draws the orb's ring arcs
        # (clockwise from 3 o'clock) so the sweep turns the same way.
        cx = g + (w / 2 if side == "right" else tl // k + w / 2)
        cy = g + h / 2
        # Only the border pixels need an angle -- a few thousand out of the
        # ~300k in the image, so walk the mask's bytes for the non-zero ones.
        sw = self.size[0]
        raw = self.ring.tobytes()
        out = bytearray(len(raw))
        sx = h / w  # squash x so the arcs spread evenly round a wide bubble
        for i, v in enumerate(raw):
            if v:
                yy, xx = divmod(i, sw)
                out[i] = round(math.degrees(math.atan2(yy - cy, (xx - cx) * sx)) % 360 / 360 * 255)
        self.ang = Image.frombytes("L", self.size, bytes(out))
        self._bases = {}

    @staticmethod
    def _arc_lut(start, span):
        """0-255 per angle code: 255 inside [start, start+span), a few
        degrees of ramp at each end so the arcs' ends don't step."""
        lut = []
        for v in range(256):
            rel = (v / 255 * 360 - start) % 360
            edge = min(rel, span - rel)
            lut.append(0 if rel >= span else min(255, round(255 * edge / 6)))
        return lut

    def _base(self, breath):
        """Everything but the moving arcs, for one breath level: the halo
        at the orb's own breathing alpha, the black body, the violet line."""
        img = Image.new("RGBA", self.size, (0, 0, 0, 0))
        strength = (150 + 79.2 * breath) / 255 * 0.8
        img.paste((*ORB_GLOW, 255), mask=self.glow.point(lambda v: round(v * strength)))
        img.paste(_rgb(FILL) + (255,), mask=self.fill)
        img.paste((*ORB_DEEP, 255), mask=self.ring)
        r, g, b, a = img.split()
        return Image.merge("RGBA", (r, g, b, a.point(lambda v: 0 if v < ALPHA_SNAP else v)))

    def frame(self, breath, angle):
        # Breath levels are pre-rendered lazily (BREATH_LEVELS of them),
        # so a frame is a copy plus the two arcs -- the halo was most of
        # the per-frame cost.
        lvl = round(breath * (BREATH_LEVELS - 1))
        if lvl not in self._bases:
            self._bases[lvl] = self._base(lvl / (BREATH_LEVELS - 1))
        img = self._bases[lvl].copy()
        long_arc = ImageChops.multiply(self.ring, self.ang.point(self._arc_lut(angle, LONG_ARC)))
        short_arc = ImageChops.multiply(self.ring, self.ang.point(self._arc_lut(angle + 180, SHORT_ARC)))
        img.paste((*ORB_LAVENDER, 255), mask=long_arc)
        img.paste((*ORB_VIOLET, 255), mask=short_arc)
        # No part-alpha pixels: Tk would blend them onto the near-black
        # colour key into a muddy dark halo on white windows (2026-10-14).
        return _flatten(img, matte=ORB_GLOW)


def _rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def _work_area(sw, sh):
    """(left, top, right, bottom) of the primary screen minus the taskbar,
    in real px -- tray.py makes the process DPI-aware before any window
    exists, so these match Tk's geometry units. Asking Windows rather than
    guessing a taskbar height means it stays right with the taskbar on any
    edge, a different size, or auto-hidden. Falls back to the whole screen
    less a typical bottom taskbar if the call isn't available."""
    try:
        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, sw, sh - 48


def _place(w, h, tl, area, margin, orb=None, gap=0, lim=0):
    """Where the bubble goes, in real px. Pure arithmetic, testable without
    Tk -- getting this wrong is how a tail ends up pointing at nothing.

    w/h: body size. tl: tail length. area: work area (left, top, right,
    bottom). orb: (x, y, size) of the orb window, or None. gap: how far the
    tail tip tucks into the orb's glow so the two read as joined. lim: how
    close to the top/bottom the tail may sit, so it never hits a corner.

    Returns (x, y, side, tail_y): x/y for the whole window including the
    tail; side is the edge the tail leaves from ("right" = bubble left of
    the orb); tail_y relative to the bubble's top. Centred on the orb, then
    clamped inside the work area -- never on the taskbar -- with the tail
    sliding along the side so it still points at the orb.
    """
    left, top, right, bottom = area
    total = w + tl
    if orb is None:
        x, y, side = right - total - margin, bottom - h - margin, "right"
        ocy = y + h // 2
    else:
        ox, oy, o = orb
        ocy = oy + o // 2
        fits_left = ox + gap - total >= left
        fits_right = ox + o - gap + total <= right
        if ox + o // 2 > (left + right) // 2:
            side = "right" if fits_left or not fits_right else "left"
        else:
            side = "left" if fits_right or not fits_left else "right"
        x = ox + gap - total if side == "right" else ox + o - gap
        y = ocy - h // 2
    x = max(left, min(x, right - total))
    y = max(top + margin, min(y, bottom - h - margin))
    tail_y = max(lim, min(ocy - y, h - lim))
    return x, y, side, tail_y


def _greeting():
    h = datetime.now().hour
    if h < 12:
        return "Morning"
    if h < 18:
        return "Afternoon"
    return "Evening"


def _due_line(t):
    """(text, color) for a task's deadline line."""
    if not t.get("due"):
        return (t.get("due_text") or "No deadline"), MUTED
    when = datetime.fromisoformat(t["due"])
    now = datetime.now()
    clock = "" if (when.hour, when.minute) == (23, 59) else f" {when:%H:%M}"
    if when < now:
        return f"Overdue · {when:%a %d %b}{clock}", BAD
    if when.date() == now.date():
        label = f"Today{clock or ' (end of day)'}"
    elif when.date() == (now + timedelta(days=1)).date():
        label = f"Tomorrow{clock}"
    else:
        label = f"{when:%a %d %b}{clock}"
    return label, (WARN if when - now <= timedelta(hours=48) else MUTED)


def _due_prefill(t):
    """What goes in the deadline box when editing -- something tasks.parse_due
    reads straight back, so saving without touching it changes nothing."""
    if t.get("due"):
        return f"{datetime.fromisoformat(t['due']):%d %b %Y %H:%M}"
    return t.get("due_text") or ""


class ReminderCard:
    def __init__(self, root, orb, tasks_module, font_family="Segoe UI", on_close=None, thinking=False):
        self.root = root
        self.thinking = thinking  # stay a "typing" bubble until closed (ThinkingBubble)
        self.orb = orb
        self.tasks = tasks_module
        self.font = font_family
        self.on_close = on_close
        self.s = _dpi_scale(root)
        self.editing = None  # a task id, "new", or None
        self.note = None  # one-line message shown in the footer after an action
        self._img = None
        self._shown = False  # first layout slides in; re-renders just resize
        self.typing = True  # opens as a "typing" bubble, then the message lands
        self._dots = 0
        self._animating = False
        self._img_key = None  # what the current bubble image was drawn for
        self._orb_seen = None  # orb position at the last layout
        self._dims = None
        self._art = None  # _BubbleArt for the current shape
        self._tick = 0  # own animation clock, only used with no orb
        self._rows_parent = None  # the scrolling frame task rows go in
        self._list_canvas = self._bar = self._edit_box = None
        self._list_view = self._list_full = 0
        self._scroll_frac = 0.0  # kept across re-renders (tick, edit, ...)

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.0)  # hidden until _slide_in fades it up
        try:
            self.win.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            pass  # Windows-only; elsewhere the corners just show the key color
        self.win.config(bg=TRANSPARENT_KEY)
        self.canvas = tk.Canvas(self.win, bg=TRANSPARENT_KEY, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.body = None
        self.win.bind("<Escape>", lambda e: self.close())
        self.render()

    # -- helpers --

    def _px(self, n):
        return round(n * self.s)

    def _f(self, size, weight="normal"):
        return (self.font, size, weight)

    def _link(self, parent, text, cmd, fg=MUTED, hover=ACCENT, size=10, weight="normal"):
        lbl = tk.Label(parent, text=text, bg=FILL, fg=fg, font=self._f(size, weight), cursor="hand2", bd=0)
        lbl.bind("<Button-1>", lambda e: cmd())
        lbl.bind("<Enter>", lambda e: lbl.config(fg=hover))
        lbl.bind("<Leave>", lambda e: lbl.config(fg=fg))
        return lbl

    def _entry(self, parent, value):
        e = tk.Entry(
            parent, bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT, relief="flat",
            font=self._f(9), highlightthickness=1, highlightbackground=DIVIDER,
            highlightcolor=ACCENT,
        )
        e.insert(0, value)
        return e

    # -- build --

    def render(self):
        if self.body is not None:
            self.body.destroy()
        self.body = tk.Frame(self.canvas, bg=FILL)
        self._img_key = None  # new body -> the canvas needs rebuilding
        if self.typing:
            self._typing_label = tk.Label(self.body, text="•", bg=FILL, fg=MUTED, font=self._f(11, "bold"), anchor="w")
            self._typing_label.pack(fill="x")
            self._layout(TYPING_W, self._px(TYPING_W - PAD * 2))
            self._tick_dots()
            if not self.thinking:
                self.win.after(TYPING_MS, self._stop_typing)
            return
        inner_w = self._px(CARD_W - PAD * 2)

        open_tasks = self.tasks.list_tasks()

        head = tk.Frame(self.body, bg=FILL)
        head.pack(fill="x")
        tk.Label(head, text=f"{_greeting()}, Chuen", bg=FILL, fg=TEXT, font=self._f(11, "bold")).pack(side="left")
        # Faded word rather than a close cross -- it shouldn't compete with
        # the per-row ✕, which deletes a reminder rather than hiding the card.
        self._link(head, "Dismiss", self.close, fg=FAINT, hover=MUTED, size=9).pack(side="right", pady=(self._px(3), 0))
        n = len(open_tasks)
        groups = {p: [t for t in open_tasks if _prio(t) == p] for p in PRIORITY_ORDER}
        if n == 0:
            sub = "Nothing on your plate right now."
        else:
            counts = ", ".join(f"{len(groups[p])} {p}" for p in PRIORITY_ORDER if groups[p])
            sub = f"You've got {n} thing{'s' if n != 1 else ''} on your plate ({counts}):"
        tk.Label(self.body, text=sub, bg=FILL, fg=MUTED, font=self._f(9), anchor="w", wraplength=inner_w, justify="left").pack(fill="x", pady=(0, self._px(2)))

        # Rows go in a scrolling viewport; header and Add link stay put.
        row_w = inner_w - self._px(BAR_W + 4)
        wrap = tk.Frame(self.body, bg=FILL)
        wrap.pack(fill="x")
        lc = tk.Canvas(wrap, bg=FILL, highlightthickness=0, bd=0, width=row_w, yscrollincrement=self._px(20))
        lc.pack(side="left")
        bar = tk.Canvas(wrap, bg=FILL, highlightthickness=0, bd=0, width=self._px(BAR_W))
        bar.pack(side="right", fill="y")
        lf = tk.Frame(lc, bg=FILL)
        lc.create_window(0, 0, anchor="nw", window=lf, width=row_w)
        self._list_canvas, self._bar, self._rows_parent = lc, bar, lf
        self._edit_box = None

        if n:
            legend = tk.Frame(self.body, bg=FILL)
            for p in PRIORITY_ORDER:
                if groups[p]:
                    tk.Label(
                        legend, text=f"●  {PRIORITY_LABEL[p]}", bg=FILL, fg=PRIORITY_COLOR[p],
                        font=self._f(8, "bold"),
                    ).pack(side="left", padx=(0, self._px(12)))
            legend.pack(fill="x", pady=(0, self._px(4)), before=wrap)

        # list_tasks() is already soonest-first (undated last).
        shown = 0
        for t in open_tasks[:MAX_ROWS]:
            if shown:
                tk.Frame(lf, bg=DIVIDER, height=1).pack(fill="x", pady=self._px(3))
            if self.editing == t["id"]:
                self._edit_row(t, row_w)
            else:
                self._task_row(t, row_w)
            shown += 1
        self._fit_list()
        if n > shown:
            tk.Label(self.body, text=f"+ {n - shown} more -- ask Denisse to list them", bg=FILL, fg=MUTED, font=self._f(8)).pack(anchor="w")

        tk.Frame(self.body, bg=DIVIDER, height=1).pack(fill="x", pady=self._px(3))
        if self.editing == "new":
            self._edit_row(None, inner_w)
        else:
            self._link(self.body, "+  Add reminder", self._start_new, fg=ACCENT, hover=TEXT, size=9).pack(anchor="w")

        if self.note:
            tk.Label(self.body, text=self.note, bg=FILL, fg=WARN, font=self._f(8), wraplength=inner_w, justify="left").pack(anchor="w", pady=(self._px(4), 0))

        # WhatsApp's little time stamp, bottom-right of the bubble.
        tk.Label(self.body, text=f"{datetime.now():%H:%M}", bg=FILL, fg=FAINT, font=self._f(7)).pack(anchor="e")

        self._layout(CARD_W, inner_w)
        self.win.after(30, self._scroll_to_edit)

    # -- scrolling list --

    def _fit_list(self):
        lc, lf = self._list_canvas, self._rows_parent
        lf.update_idletasks()
        full = max(1, lf.winfo_reqheight())
        left, top, right, bottom = _work_area(self.win.winfo_screenwidth(), self.win.winfo_screenheight())
        view = min(full, max(self._px(120), round((bottom - top) * LIST_MAX_FRAC)))
        self._list_view, self._list_full = view, full
        lc.config(height=view, scrollregion=(0, 0, 0, full), yscrollcommand=lambda *a: self._draw_bar())
        lc.yview_moveto(self._scroll_frac)
        self._bind_wheel(lc)
        self._bar.config(height=view)
        self._bar.bind("<Button-1>", self._bar_drag)
        self._bar.bind("<B1-Motion>", self._bar_drag)
        self._draw_bar()

    def _bind_wheel(self, w):
        w.bind("<MouseWheel>", self._on_wheel)
        for c in w.winfo_children():
            self._bind_wheel(c)

    def _on_wheel(self, e):
        if self._list_full > self._list_view:
            notches = int(e.delta / 120)
            step = -2 * notches if notches else (-1 if e.delta > 0 else 1)  # touchpads send small deltas
            self._list_canvas.yview_scroll(step, "units")
            self._scroll_frac = self._list_canvas.yview()[0]
            self._draw_bar()
        return "break"

    def _bar_drag(self, e):
        if self._list_full <= self._list_view:
            return
        top, bot = self._list_canvas.yview()
        span = bot - top
        self._list_canvas.yview_moveto(max(0.0, min(1.0 - span, e.y / self._list_view - span / 2)))
        self._scroll_frac = self._list_canvas.yview()[0]
        self._draw_bar()

    def _draw_bar(self):
        """Slim thumb on a hairline track, only when there's more to see."""
        try:
            bar = self._bar
            bar.delete("all")
            if self._list_full <= self._list_view:
                return
            h, w = self._list_view, self._px(BAR_W)
            top, bot = self._list_canvas.yview()
            bar.create_rectangle(w // 2, 0, w // 2 + 1, h, fill=DIVIDER, outline="")
            y0 = round(top * h)
            bar.create_rectangle(0, y0, w, max(y0 + self._px(16), round(bot * h)), fill=ACCENT, outline="")
        except (tk.TclError, AttributeError):
            pass

    def _scroll_to_edit(self):
        """Bring the row being edited into view -- needs real geometry, so
        it runs just after layout rather than while the rows are built."""
        try:
            if self._edit_box is None or self._list_full <= self._list_view:
                return
            y = self._edit_box.winfo_y()
            self._list_canvas.yview_moveto(max(0, y - self._px(8)) / self._list_full)
            self._scroll_frac = self._list_canvas.yview()[0]
            self._draw_bar()
        except (tk.TclError, AttributeError):
            pass

    def _task_row(self, t, inner_w):
        row = tk.Frame(self._rows_parent, bg=FILL)
        row.pack(fill="x")
        color = PRIORITY_COLOR[_prio(t)]
        tk.Frame(row, bg=color, width=self._px(3)).pack(side="left", fill="y", padx=(0, self._px(6)))
        check = self._link(row, "○", lambda: self._done(t), fg=color, hover=GOOD, size=12)
        check.pack(side="left", anchor="n", padx=(0, self._px(6)))
        actions = tk.Frame(row, bg=FILL)
        actions.pack(side="right", anchor="n")
        self._link(actions, "✎", lambda: self._start_edit(t), size=10).pack(side="left", padx=(self._px(4), 0))
        self._link(actions, "✕", lambda: self._delete(t), hover=BAD, size=9).pack(side="left", padx=(self._px(6), 0))
        mid = tk.Frame(row, bg=FILL)
        mid.pack(side="left", fill="x", expand=True)
        text_w = inner_w - self._px(71)  # check, actions, and the priority stripe
        tk.Label(mid, text=t["text"], bg=FILL, fg=TEXT, font=self._f(9), wraplength=text_w, justify="left", anchor="w").pack(fill="x")
        due, color = _due_line(t)
        tk.Label(mid, text=due, bg=FILL, fg=color, font=self._f(8), anchor="w").pack(fill="x")

    def _edit_row(self, t, inner_w):
        # Editing an existing task happens inside the scrolling list; the
        # "Add reminder" row (t is None) sits below it in the card body.
        box = tk.Frame(self._rows_parent if t is not None else self.body, bg=FILL)
        box.pack(fill="x")
        if t is not None:
            self._edit_box = box
        tk.Label(box, text="Reminder", bg=FILL, fg=MUTED, font=self._f(8)).pack(anchor="w")
        text_e = self._entry(box, t["text"] if t else "")
        text_e.pack(fill="x", ipady=self._px(2))
        tk.Label(box, text="When (e.g. friday 5pm, tomorrow 9am) -- optional", bg=FILL, fg=MUTED, font=self._f(8)).pack(anchor="w", pady=(self._px(4), 0))
        due_e = self._entry(box, _due_prefill(t) if t else "")
        due_e.pack(fill="x", ipady=self._px(2))
        tk.Label(box, text="Priority", bg=FILL, fg=MUTED, font=self._f(8)).pack(anchor="w", pady=(self._px(4), 0))
        chips_row = tk.Frame(box, bg=FILL)
        chips_row.pack(anchor="w")
        chips = {}

        def pick(p):
            self._edit_prio = p
            for q, lbl in chips.items():
                on = q == p
                lbl.config(bg=PRIORITY_COLOR[q] if on else FILL, fg=FILL if on else PRIORITY_COLOR[q])

        for p in PRIORITY_ORDER:
            lbl = tk.Label(
                chips_row, text=PRIORITY_LABEL[p], bg=FILL, fg=PRIORITY_COLOR[p], font=self._f(8, "bold"),
                padx=self._px(7), pady=self._px(1), cursor="hand2",
                highlightthickness=1, highlightbackground=PRIORITY_COLOR[p],
            )
            lbl.pack(side="left", padx=(0, self._px(6)))
            lbl.bind("<Button-1>", lambda e, p=p: pick(p))
            chips[p] = lbl
        pick(_prio(t) if t else "medium")

        btns = tk.Frame(box, bg=FILL)
        btns.pack(anchor="e", pady=(self._px(5), 0))
        save = lambda: self._save(t, text_e.get(), due_e.get())
        self._link(btns, "Cancel", self._cancel_edit, size=9).pack(side="left", padx=(0, self._px(10)))
        self._link(btns, "Save", save, fg=ACCENT, hover=TEXT, size=9, weight="bold").pack(side="left")
        for e in (text_e, due_e):
            e.bind("<Return>", lambda ev: (save(), "break")[1])
            e.bind("<Escape>", lambda ev: (self._cancel_edit(), "break")[1])

        def _focus():
            try:
                self.win.focus_force()
                text_e.focus_set()
                text_e.icursor("end")
            except tk.TclError:
                pass  # re-rendered before this fired
        self.win.after(10, _focus)

    def _tick_dots(self):
        if not self.typing:
            return
        try:
            self._dots = self._dots % 3 + 1
            self._typing_label.config(text=" ".join("•" * self._dots))
            self.win.after(260, self._tick_dots)
        except tk.TclError:
            pass

    def _stop_typing(self):
        self.typing = False
        try:
            self.render()
        except tk.TclError:
            pass  # dismissed while still "typing"

    def _orb_box(self):
        try:
            return self.orb.win.winfo_x(), self.orb.win.winfo_y(), self.orb.total_px
        except Exception:
            return None  # no orb (or it's gone) -- fall back to the corner

    def _layout(self, card_w, inner_w):
        """Size the bubble to the body and put it beside the orb. Also what
        _follow calls when the orb moves -- it only redraws the bubble image
        when the side or tail position actually changed, so dragging is
        just a window move."""
        self._dims = (card_w, inner_w)
        self.body.update_idletasks()
        w = self._px(card_w)
        h = self.body.winfo_reqheight() + self._px(PAD) * 2
        tl = self._px(TAIL_LEN)
        area = _work_area(self.win.winfo_screenwidth(), self.win.winfo_screenheight())
        self._orb_seen = self._orb_box()
        x, y, side, tail_y = _place(
            w, h, tl, area, self._px(MARGIN), orb=self._orb_seen,
            gap=self._px(11),  # ~ the orb's glow pad, so the tip meets the orb itself
            lim=self._px(RADIUS) + self._px(TAIL_BASE) // 2,
        )

        # The window is the bubble plus a halo margin all round; x/y from
        # _place are the bubble's, so the window sits g up and left of them.
        g = self._px(GLOW)
        key = (w, h, side, tail_y)
        if key != self._img_key:
            self._img_key = key
            self._art = _BubbleArt(w, h, side, tail_y, self.s, g)
            self._img = ImageTk.PhotoImage(self._art.frame(*self._phase()))
            self.canvas.delete("all")
            self.canvas.config(width=w + tl + 2 * g, height=h + 2 * g)
            self.canvas.create_image(0, 0, anchor="nw", image=self._img)
            body_x = g + (0 if side == "right" else tl) + self._px(PAD)
            self.canvas.create_window(body_x, g + self._px(PAD), anchor="nw", window=self.body, width=inner_w)
        size = f"{w + tl + 2 * g}x{h + 2 * g}"
        if self._shown:
            if not self._animating:
                self.win.geometry(f"{size}+{x - g}+{y - g}")
        else:
            self._shown = True
            self._slide_in(size, x - g, y - g, side)
            self._follow()
            self._alive()
        self.win.deiconify()
        self.win.lift()

    def _phase(self):
        """(breath 0-1, ring angle) for this instant. Breath is read off the
        orb's own frame counter so the halo pulses in step with it. The arcs
        run on the clock at SWEEP_SPEED x the orb ring's rate -- he asked
        for them 60% slower than the orb's (2026-10-07), so they can't
        share its frame counter, which wraps every full turn."""
        angle = time.monotonic() * ORB_RING_DEG_PER_S * SWEEP_SPEED % 360
        try:
            breath = _breath_curve(self.orb._frame_i, BREATH_FRAMES)
        except Exception:  # no orb -- keep time ourselves
            self._tick += 1
            breath = _breath_curve(self._tick % BREATH_FRAMES, BREATH_FRAMES)
        return breath, angle

    def _alive(self):
        """Redraw the border's breathing glow and sweeping ring."""
        try:
            if not self.win.winfo_exists():
                return
            self._img.paste(self._art.frame(*self._phase()))
            self.win.after(ALIVE_MS, self._alive)
        except tk.TclError:
            pass  # closed

    def _follow(self):
        """Keep the bubble beside the orb while he drags it. Polls the orb's
        position rather than hooking floating_orb's drag handlers, so the
        orb doesn't need to know the card exists."""
        try:
            if not self.win.winfo_exists():
                return
            if not self._animating and self._orb_box() != self._orb_seen:
                self._layout(*self._dims)
            self.win.after(FOLLOW_MS, self._follow)
        except tk.TclError:
            pass  # closed

    def _slide_in(self, size, x, y, side):
        """Slide SLIDE px out of the orb and fade in, ease-out -- the
        message coming from the orb rather than just appearing."""
        d = self._px(SLIDE) * (1 if side == "right" else -1)
        self._animating = True

        def step(i):
            try:
                t = i / ANIM_STEPS
                ease = 1 - (1 - t) ** 3
                self.win.geometry(f"{size}+{x + round(d * (1 - ease))}+{y}")
                self.win.attributes("-alpha", max(0.0, min(1.0, ease)))
                if i < ANIM_STEPS:
                    self.win.after(ANIM_MS // ANIM_STEPS, step, i + 1)
                else:
                    self._animating = False
                    self._layout(*self._dims)  # catch up if the orb moved meanwhile
            except tk.TclError:
                pass  # closed mid-animation

        step(0)

    # -- actions --

    def _done(self, t):
        self.tasks.complete_task(t["id"])
        self.note = None
        self.editing = None
        self.render()

    def _delete(self, t):
        self.tasks.remove_task(t["id"])
        self.note = None
        self.editing = None
        self.render()

    def _start_edit(self, t):
        self.editing = t["id"]
        self.note = None
        self.render()

    def _start_new(self):
        self.editing = "new"
        self.note = None
        self.render()

    def _cancel_edit(self):
        self.editing = None
        self.render()

    def _save(self, t, text, due):
        text, due = text.strip(), due.strip()
        if not text:
            self.note = "A reminder needs some text."
            self.render()
            return
        # Untouched deadline box -> leave the deadline alone, so the original
        # phrase ("before friday") isn't swapped for the prefill's
        # reformatted date just because the text was edited.
        unchanged = t is not None and due == _due_prefill(t)
        prio = getattr(self, "_edit_prio", None)
        if t is None:
            saved = self.tasks.add_task(text, due, priority=prio or "medium")
        else:
            saved = self.tasks.update_task(t["id"], text=text, due=None if unchanged else due, priority=prio)
        self.note = None
        if saved and due and not unchanged and not saved.get("due"):
            self.note = f'Couldn\'t read "{due}" as a date -- kept it as a note instead.'
        self.editing = None
        self.render()

    def refresh(self):
        """Re-read the task list (it may have changed via chat or Obsidian)."""
        if self.editing is None and not self.typing:
            self.render()
        self.win.lift()

    def close(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if self.on_close:
            self.on_close()


class ThinkingBubble(ReminderCard):
    """Just the animated "typing" bubble out of the orb, held up for as long
    as Denisse is composing a reply -- asked for 2026-10-07 after he saw the
    reminder card's brief typing intro. tray.py opens it when a reply starts
    (typed chat or "hey Denisse") and closes it when the reply lands. Same
    breathing, sweeping border as the card, so the two read as one thing."""

    def __init__(self, root, orb, font_family="Segoe UI", on_close=None):
        super().__init__(root, orb, None, font_family=font_family, on_close=on_close, thinking=True)

    # -- the reply landing in the same bubble --
    #
    # Asked 2026-10-14: "whenever she speaks I want her message to pop up
    # like the ... animation so I can see them". So the "• • •" bubble
    # doesn't just vanish when the reply is ready -- it turns into the
    # reply, the way a text arrives after the typing dots. tray.py calls
    # deliver() as she starts speaking (chat, "hey Denisse", the news
    # briefing). It then stays up long enough to read and closes itself;
    # a click on it closes it sooner.

    message = None
    REPLY_W = 290  # logical px
    REPLY_MAX_CHARS = 600  # the chat keeps the full text; this is the glance

    def deliver(self, text):
        """Turn this bubble into the reply. Returns True if it is on screen.

        False means the window had already gone -- closed by hand, or by
        the reminder card / a second question taking the bubble's place
        during the typing pause. This used to swallow the TclError and
        return nothing, so the caller believed the reply had landed when
        in fact it was shown nowhere at all; with the chat popup closed
        and TTS off that is a whole exchange lost. tray._deliver_reply
        rebuilds on a False.
        """
        try:
            if not self.win.winfo_exists():
                return False
        except tk.TclError:
            return False
        self.thinking = self.typing = False
        text = " ".join((text or "").split())
        if len(text) > self.REPLY_MAX_CHARS:
            text = text[: self.REPLY_MAX_CHARS - 1].rstrip() + "…"
        self.message = text
        try:
            self.render()
            # ~ reading speed plus a margin, never under 6s or over 45s
            ms = min(45_000, max(6_000, 3_000 + 70 * len(text)))
            self.win.after(ms, self._expire)
        except tk.TclError:
            self.message = None  # nothing is up -- don't claim it landed
            return False
        return True

    def _expire(self):
        # Tk doesn't cancel after() when the window goes -- only close
        # if this bubble is still up, or on_close would run twice.
        try:
            if self.win.winfo_exists():
                self.close()
        except tk.TclError:
            pass

    def render(self):
        if self.message is None:
            return super().render()
        if self.body is not None:
            self.body.destroy()
        self.body = tk.Frame(self.canvas, bg=FILL, cursor="hand2")
        self._img_key = None
        inner_w = self._px(self.REPLY_W - PAD * 2)
        msg = tk.Label(
            self.body, text=self.message, bg=FILL, fg=TEXT, font=self._f(10),
            anchor="w", wraplength=inner_w, justify="left", cursor="hand2",
        )
        msg.pack(fill="x")
        stamp = tk.Label(self.body, text=f"{datetime.now():%H:%M}", bg=FILL, fg=FAINT, font=self._f(7), cursor="hand2")
        stamp.pack(anchor="e")
        for w in (self.body, msg, stamp):
            w.bind("<Button-1>", lambda e: self.close())
        self._layout(self.REPLY_W, inner_w)

    def refresh(self):
        self.win.lift()
