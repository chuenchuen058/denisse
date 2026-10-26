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


class OrbControls:
    def __init__(self, root, orb, on_chat, on_voice, on_reminders,
                 voice_active, font_family="Segoe UI"):
        self.root, self.orb = root, orb
        self.voice_active = voice_active
        self._job = None
        self._closed = False
        self._visible = False
        self._last_hover = 0.0
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#4c3575", padx=1, pady=1)
        body = tk.Frame(self.win, bg="#0d0916")
        body.pack()
        self.buttons = {}
        size = max(18, round(20 * orb._scale))
        self._icons = {label: ImageTk.PhotoImage(_icon(label, size, "#ddd0f5"))
                       for label in ("Chat", "Voice", "Reminders")}
        self._voice_on = ImageTk.PhotoImage(_icon("Voice", size, "#74ebc5"))
        for label, action in (("Chat", on_chat), ("Voice", on_voice),
                              ("Reminders", on_reminders)):
            button = tk.Button(
                body, text=label, image=self._icons[label], command=lambda fn=action: self._invoke(fn),
                font=(font_family, 9), bg="#0d0916", fg="#ddd0f5",
                activebackground="#38204f", activeforeground="#ffffff",
                relief="flat", bd=0, padx=9, pady=6, cursor="hand2",
                highlightthickness=0, takefocus=True,
            )
            button.pack(side="left")
            button.bind("<Enter>", lambda e, b=button: b.config(bg="#281a3b"))
            button.bind("<Leave>", lambda e, b=button: b.config(bg="#0d0916"))
            self.buttons[label] = button
        self.win.update_idletasks()
        self._follow()

    def _invoke(self, action):
        self.orb.react("happy", 1.0)
        action()
        self._refresh_voice()

    def _refresh_voice(self):
        active = self.voice_active()
        # Keep the label and width stable so toggling never shifts a target.
        self.buttons["Voice"].configure(
            image=self._voice_on if active else self._icons["Voice"],
            fg="#74ebc5" if active else "#ddd0f5",
            relief="sunken" if active else "flat",
        )

    def _follow(self):
        self._job = None
        if self._closed:
            return
        try:
            if not self.orb.win.winfo_exists():
                self.close()
                return
            w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
            ox, oy = self.orb.win.winfo_x(), self.orb.win.winfo_y()
            left, top, right, bottom = _work_area(
                self.win.winfo_screenwidth(), self.win.winfo_screenheight())
            gap = round(4 * self.orb._scale)
            x = max(left, min(ox + self.orb.total_px // 2 - w // 2, right - w))
            y = oy + self.orb.total_px + gap
            if y + h > bottom:
                y = oy - h - gap
            y = max(top, min(y, bottom - h))
            geo = f"{w}x{h}+{x}+{y}"
            if self.win.geometry() != geo:
                self.win.geometry(geo)
            px, py = self.win.winfo_pointerxy()
            self._update_visibility(px, py, time.monotonic(), (x, y, w, h), (ox, oy))
            self._refresh_voice()
            self._job = self.root.after(100, self._follow)
        except tk.TclError:
            self.close()

    def _update_visibility(self, px, py, now, dock_rect, orb_pos):
        x, y, w, h = dock_rect
        ox, oy = orb_pos
        over_orb = ox <= px < ox + self.orb.total_px and oy <= py < oy + self.orb.total_px
        over_dock = self._visible and x <= px < x + w and y <= py < y + h
        if over_orb or over_dock:
            self._last_hover = now
            if not self._visible:
                self.win.deiconify()
                self._visible = True
        elif self._visible and now - self._last_hover > 0.4:
            # Enough time to cross the transparent gap without losing targets.
            self.win.withdraw()
            self._visible = False

    def close(self):
        self._closed = True
        if self._job is not None:
            self.root.after_cancel(self._job)
            self._job = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass
