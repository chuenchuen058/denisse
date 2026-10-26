"""Hover-revealed Chat / Voice / Reminders icons beneath Denisse's orb."""

import time
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

from reminder_card import _work_area


def _icon(label, size, color):
    """Small antialiased outline icons, independent of installed symbol fonts."""
    image = Image.new("RGBA", (96, 96))
    d = ImageDraw.Draw(image)
    if label == "Chat":
        d.rounded_rectangle((17, 19, 79, 66), radius=12, outline=color, width=6)
        d.line((29, 66, 25, 79, 44, 66), fill=color, width=6, joint="curve")
    elif label == "Voice":
        d.rounded_rectangle((36, 13, 60, 57), radius=12, outline=color, width=6)
        d.arc((24, 29, 72, 71), 0, 180, fill=color, width=6)
        d.line((48, 71, 48, 83), fill=color, width=6)
        d.line((35, 83, 61, 83), fill=color, width=6)
    else:
        d.arc((24, 17, 72, 65), 180, 360, fill=color, width=6)
        d.line((24, 41, 24, 60, 16, 70, 80, 70, 72, 60, 72, 41),
               fill=color, width=6, joint="curve")
        d.arc((39, 69, 57, 85), 0, 180, fill=color, width=6)
        d.line((48, 11, 48, 17), fill=color, width=6)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def _dock_frame(scale, hovered=None, active=False, pressed=None):
    """Render one compact pill; shared by the live dock and visual previews."""
    ss = 3
    w, h = round(144 * scale), round(46 * scale)
    image = Image.new("RGBA", (w * ss, h * ss))
    d = ImageDraw.Draw(image)
    k = scale * ss
    def box(coords):
        return tuple(round(v * k) for v in coords)
    d.rounded_rectangle(box((2, 2, 142, 44)), radius=round(21*k),
                        fill="#100d19", outline="#49345f", width=max(1, round(k)))
    d.line(box((30, 3, 114, 3)), fill="#6c4b89", width=max(1, round(k/2)))
    for i, label in enumerate(("Chat", "Voice", "Reminders")):
        cx = 28 + i * 44
        is_active = label == "Voice" and active
        if label == hovered or is_active:
            fill = "#462c60" if label == pressed else "#2d203f" if label == hovered else "#142d2c"
            d.rounded_rectangle(box((cx-18, 5, cx+18, 41)),
                                radius=round(14*k), fill=fill)
        color = "#80e4c4" if is_active else "#faf2ff" if label == hovered else "#c2b1d5"
        icon = _icon(label, round(22*k), color)
        image.alpha_composite(icon, (round((cx-11)*k), round(11*k)))
        if is_active:
            d.ellipse(box((cx+9, 9, cx+12, 12)), fill="#80e4c4")
    return image.resize((w, h), Image.Resampling.LANCZOS)


class OrbControls:
    LABELS = ("Chat", "Voice", "Reminders")

    def __init__(self, root, orb, on_chat, on_voice, on_reminders,
                 voice_active, font_family="Segoe UI"):
        self.root, self.orb = root, orb
        self.voice_active = voice_active
        self.actions = dict(zip(self.LABELS, (on_chat, on_voice, on_reminders)))
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
        self.win.title("Denisse controls")
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#010203")
        self.w, self.h = round(144*orb._scale), round(46*orb._scale)
        self.canvas = tk.Canvas(self.win, width=self.w, height=self.h,
                                bg="#010203", bd=0, highlightthickness=0,
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
            self.win.attributes("-transparentcolor", "#010203")
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Right>", lambda e: self._keyboard_step(1))
        self.canvas.bind("<Left>", lambda e: self._keyboard_step(-1))
        self.canvas.bind("<Return>", lambda e: self._invoke(self._hovered or "Chat"))
        self.canvas.bind("<space>", lambda e: self._invoke(self._hovered or "Chat"))
        self.canvas.bind("<Escape>", lambda e: self._hide())
        self.tip = tk.Toplevel(root)
        self.tip.withdraw()
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self._tip_label = tk.Label(self.tip, bg="#191222", fg="#e6d9f4",
                                   font=(font_family, 8), padx=8, pady=4)
        self._tip_label.pack()
        self.win.update_idletasks()
        self._follow()

    def _button_at(self, x, y):
        x, y = x/self.orb._scale, y/self.orb._scale
        if not 5 <= y <= 41:
            return None
        for i, label in enumerate(self.LABELS):
            if 10+i*44 <= x <= 46+i*44:
                return label
        return None

    def _press(self, event):
        self._pressed = self._button_at(event.x, event.y)
        self.canvas.focus_set()
        self._draw()

    def _release(self, event):
        label, self._pressed = self._pressed, None
        if label is not None and label == self._button_at(event.x, event.y):
            self._invoke(label)
        self._draw()

    def _invoke(self, label):
        self.orb.react("happy", 1.0)
        self.actions[label]()
        self.tip.withdraw()
        self._draw()

    def _keyboard_step(self, delta):
        i = self.LABELS.index(self._hovered) if self._hovered else 0
        self._hovered = self.LABELS[(i+delta) % 3]
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
                self._cache[state] = ImageTk.PhotoImage(_flatten(frame, matte=(16, 13, 25)))
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
            w, h = self.w, self.h
            ox, oy = self.orb.win.winfo_x(), self.orb.win.winfo_y()
            left, top, right, bottom = _work_area(
                self.win.winfo_screenwidth(), self.win.winfo_screenheight())
            gap = round(2 * self.orb._scale)
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
            if hovered and now-self._hover_since > .6:
                text = "Voice: listening on" if hovered == "Voice" and self.voice_active() else hovered
                self._tip_label.config(text=text)
                self.tip.update_idletasks()
                tw, th = self.tip.winfo_reqwidth(), self.tip.winfo_reqheight()
                tx = max(left, min(px-tw//2, right-tw))
                ty = y+h+3 if y+h+th+3 < bottom else y-th-3
                self.tip.geometry(f"+{tx}+{max(top, ty)}")
                self.tip.deiconify()
            self._draw()
            self._job = self.root.after(75, self._follow)
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
        elif self._visible and now-self._last_hover > .4:
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
