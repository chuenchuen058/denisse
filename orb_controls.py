"""Hover-revealed Chat / Voice / Reminders icons beneath Denisse's orb.

Colours, sizes, timings, the buttons, their labels and the icon shapes are
in ui_theme.json under "dock" (Rule 0), validated by ui_theme.py. A button is identified by
its action (chat, voice, reminders -- which of tray's callbacks it calls);
its label and icon are data.
"""

import time
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

import ui_theme
from reminder_card import _work_area

def _dock() -> dict:
    return ui_theme.get("dock")


def _icon(icon, size, color):
    """Small antialiased outline icons, independent of installed symbol fonts:
    the strokes of dock.glyphs.shapes[icon], drawn on the glyph canvas and
    scaled down to `size`."""
    glyphs = _dock()["glyphs"]
    n, w = glyphs["canvas"], glyphs["stroke"]
    image = Image.new("RGBA", (n, n))
    d = ImageDraw.Draw(image)
    for stroke in glyphs["shapes"][icon]:
        op, xy = stroke["op"], tuple(stroke["xy"])
        if op == "rounded_rectangle":
            d.rounded_rectangle(xy, radius=stroke["radius"], outline=color, width=w)
        elif op == "arc":
            d.arc(xy, stroke["start"], stroke["end"], fill=color, width=w)
        else:  # line
            d.line(xy, fill=color, width=w, joint="curve")
    return image.resize((size, size), Image.Resampling.LANCZOS)


def _dock_size(scale):
    lay = _dock()["layout"]
    return round(lay["width"] * scale), round(lay["height"] * scale)


def _dock_frame(scale, hovered=None, active=False, pressed=None):
    """Render one compact pill; shared by the live dock and visual previews.
    hovered/pressed are button actions; active lights the voice button."""
    dock = _dock()
    lay, col = dock["layout"], dock["colours"]
    ss = lay["supersample"]
    w, h = _dock_size(scale)
    image = Image.new("RGBA", (w * ss, h * ss))
    d = ImageDraw.Draw(image)
    k = scale * ss
    def box(coords):
        return tuple(round(v * k) for v in coords)
    inset, lw, lh = lay["inset"], lay["width"], lay["height"]
    d.rounded_rectangle(box((inset, inset, lw - inset, lh - inset)), radius=round(lay["radius"]*k),
                        fill=col["fill"], outline=col["outline"],
                        width=max(1, round(k * lay["outline_width"])))
    hy = lay["highlight_y"]
    d.line(box((lay["highlight_inset"], hy, lw - lay["highlight_inset"], hy)),
           fill=col["highlight"], width=max(1, round(k * lay["highlight_width"])))
    top, bottom, half = lay["button_top"], lh - lay["button_top"], lay["half_width"]
    icon_px = lay["icon_size"]
    for i, button in enumerate(dock["buttons"]):
        action = button["action"]
        cx = lay["first_x"] + i * lay["pitch"]
        is_active = action == "voice" and active
        if action == hovered or is_active:
            fill = (col["pressed_bg"] if action == pressed
                    else col["hover_bg"] if action == hovered else col["active_bg"])
            d.rounded_rectangle(box((cx-half, top, cx+half, bottom)),
                                radius=round(lay["button_radius"]*k), fill=fill)
        color = col["icon_active"] if is_active else col["icon_hover"] if action == hovered else col["icon"]
        icon = _icon(button["icon"], round(icon_px*k), color)
        image.alpha_composite(icon, (round((cx-icon_px/2)*k), round(lay["icon_top"]*k)))
        if is_active:
            dx, dy, ds = lay["dot_dx"], lay["dot_y"], lay["dot_size"]
            d.ellipse(box((cx+dx, dy, cx+dx+ds, dy+ds)), fill=col["icon_active"])
    return image.resize((w, h), Image.Resampling.LANCZOS)


def _button_at(scale, x, y):
    """The action of the button under dock-relative pixel (x, y), or None."""
    dock = _dock()
    lay = dock["layout"]
    x, y = x/scale, y/scale
    if not lay["button_top"] <= y <= lay["height"] - lay["button_top"]:
        return None
    for i, button in enumerate(dock["buttons"]):
        cx = lay["first_x"] + i * lay["pitch"]
        if cx - lay["half_width"] <= x <= cx + lay["half_width"]:
            return button["action"]
    return None


def _tip_text(action, voice_on):
    """Tooltip for a button: its label, or the voice_on_tip wording."""
    dock = _dock()
    label = next(b["label"] for b in dock["buttons"] if b["action"] == action)
    if action == "voice" and voice_on:
        return dock["voice_on_tip"].format(label=label)
    return label


class OrbControls:
    def __init__(self, root, orb, on_chat, on_voice, on_reminders,
                 voice_active, font_family=None):
        dock = _dock()
        lay, col = dock["layout"], dock["colours"]
        key = ui_theme.colour("transparent_key")
        self.root, self.orb = root, orb
        self.voice_active = voice_active
        callbacks = {"chat": on_chat, "voice": on_voice, "reminders": on_reminders}
        self.order = tuple(b["action"] for b in dock["buttons"])
        self.actions = {a: callbacks[a] for a in self.order}
        self._job = None
        self._closed = False
        self._visible = False
        self._last_hover = 0.0
        self._hovered = self._pressed = None
        self._hover_since = 0.0
        self._drawn = None
        self._cache = {}
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.title(dock["title"])
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=key)
        self.w, self.h = _dock_size(orb._scale)
        self.canvas = tk.Canvas(self.win, width=self.w, height=self.h,
                                bg=key, bd=0, highlightthickness=0,
                                takefocus=True, cursor="hand2")
        self.canvas.pack()
        self._image_id = self.canvas.create_image(0, 0, anchor="nw")
        self._painter = None
        try:
            from layered import LayeredPainter, to_bgra
            self._painter = LayeredPainter(self.win, (self.w, self.h))
            self._convert = to_bgra
            if not self._painter.show(to_bgra(_dock_frame(orb._scale))):
                raise OSError("Dock alpha painter unavailable")
        except Exception:
            if self._painter is not None:
                self._painter.close()
            self._painter = None
            self.win.attributes("-transparentcolor", key)
        first = self.order[0]
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Right>", lambda e: self._keyboard_step(1))
        self.canvas.bind("<Left>", lambda e: self._keyboard_step(-1))
        self.canvas.bind("<Return>", lambda e: self._invoke(self._hovered or first))
        self.canvas.bind("<space>", lambda e: self._invoke(self._hovered or first))
        self.canvas.bind("<Escape>", lambda e: self._hide())
        self.tip = tk.Toplevel(root)
        self.tip.withdraw()
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self._tip_label = tk.Label(self.tip, bg=col["tip_bg"], fg=col["tip_fg"],
                                   font=(font_family or ui_theme.get("fonts")["family"], lay["tip_font_size"]),
                                   padx=lay["tip_padx"], pady=lay["tip_pady"])
        self._tip_label.pack()
        self.win.update_idletasks()
        self._follow()

    def _button_at(self, x, y):
        return _button_at(self.orb._scale, x, y)

    def _press(self, event):
        self._pressed = self._button_at(event.x, event.y)
        self.canvas.focus_set()
        self._draw()

    def _release(self, event):
        action, self._pressed = self._pressed, None
        if action is not None and action == self._button_at(event.x, event.y):
            self._invoke(action)
        self._draw()

    def _invoke(self, action):
        act = ui_theme.get("interaction")
        self.orb.react(act["dock_mood"], act["react_dock_s"])
        self.actions[action]()
        self.tip.withdraw()
        self._draw()

    def _keyboard_step(self, delta):
        i = self.order.index(self._hovered) if self._hovered else 0
        self._hovered = self.order[(i+delta) % len(self.order)]
        self._draw()
        return "break"

    def _draw(self):
        state = (self._hovered, bool(self.voice_active()), self._pressed)
        if state == self._drawn:
            return
        if state not in self._cache:
            frame = _dock_frame(self.orb._scale, *state)
            if self._painter is not None:
                self._cache[state] = self._convert(frame)
            else:
                from floating_orb import _flatten
                matte = ui_theme.rgb(_dock()["colours"]["fill"])
                self._cache[state] = ImageTk.PhotoImage(_flatten(frame, matte=matte))
        if self._painter is not None:
            self._painter.show(self._cache[state])
        else:
            self.canvas.itemconfig(self._image_id, image=self._cache[state])
        self._drawn = state

    def _hide(self):
        self.win.withdraw()
        self.tip.withdraw()
        self._visible = False
        self._hovered = self._pressed = None

    def _follow(self):
        self._job = None
        if self._closed:
            return
        try:
            if not self.orb.win.winfo_exists():
                self.close()
                return
            dock = _dock()
            lay, timing = dock["layout"], dock["timing"]
            w, h = self.w, self.h
            ox, oy = self.orb.win.winfo_x(), self.orb.win.winfo_y()
            left, top, right, bottom = _work_area(
                self.win.winfo_screenwidth(), self.win.winfo_screenheight())
            gap = round(lay["gap"] * self.orb._scale)
            x = max(left, min(ox + self.orb.total_px // 2 - w // 2, right - w))
            y = oy + self.orb.total_px + gap
            if y + h > bottom:
                y = oy - h - gap
            y = max(top, min(y, bottom - h))
            geo = f"{w}x{h}+{x}+{y}"
            if self.win.geometry() != geo:
                self.win.geometry(geo)
            px, py = self.win.winfo_pointerxy()
            now = time.monotonic()
            self._update_visibility(px, py, now, (x, y, w, h), (ox, oy))
            hovered = self._button_at(px-x, py-y) if self._visible else None
            if hovered != self._hovered:
                self._hovered = hovered
                self._hover_since = now
                self.tip.withdraw()
            if hovered and now-self._hover_since > timing["tip_delay_s"]:
                self._tip_label.config(text=_tip_text(hovered, self.voice_active()))
                self.tip.update_idletasks()
                tw, th = self.tip.winfo_reqwidth(), self.tip.winfo_reqheight()
                tg = lay["tip_gap"]
                tx = max(left, min(px-tw//2, right-tw))
                ty = y+h+tg if y+h+th+tg < bottom else y-th-tg
                self.tip.geometry(f"+{tx}+{max(top, ty)}")
                self.tip.deiconify()
            self._draw()
            self._job = self.root.after(timing["follow_ms"], self._follow)
        except tk.TclError:
            self.close()

    def _update_visibility(self, px, py, now, dock_rect, orb_pos):
        x, y, w, h = dock_rect
        ox, oy = orb_pos
        over_orb = ox <= px < ox+self.orb.total_px and oy <= py < oy+self.orb.total_px
        over_dock = self._visible and x <= px < x+w and y <= py < y+h
        if over_orb or over_dock:
            self._last_hover = now
            if not self._visible:
                self.win.deiconify()
                self._visible = True
        elif self._visible and now-self._last_hover > _dock()["timing"]["hide_delay_s"]:
            self._hide()

    def close(self):
        self._closed = True
        if self._job is not None:
            self.root.after_cancel(self._job)
            self._job = None
        if self._painter is not None:
            self._painter.close()
            self._painter = None
        try:
            self.tip.destroy()
            self.win.destroy()
        except tk.TclError:
            pass
