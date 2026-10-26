"""Always-visible Chat / Voice / Reminders dock beneath Denisse's orb."""

import tkinter as tk

from reminder_card import _work_area


class OrbControls:
    def __init__(self, root, orb, on_chat, on_voice, on_reminders,
                 voice_active, font_family="Segoe UI"):
        self.root, self.orb = root, orb
        self.voice_active = voice_active
        self._job = None
        self._closed = False
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#4c3575", padx=1, pady=1)
        body = tk.Frame(self.win, bg="#0d0916")
        body.pack()
        self.buttons = {}
        for label, action in (("Chat", on_chat), ("Voice", on_voice),
                              ("Reminders", on_reminders)):
            button = tk.Button(
                body, text=label, command=lambda fn=action: self._invoke(fn),
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
        self.win.deiconify()

    def _invoke(self, action):
        self.orb.react("happy", 1.0)
        action()
        self._refresh_voice()

    def _refresh_voice(self):
        active = self.voice_active()
        # Keep the label and width stable so toggling never shifts a target.
        self.buttons["Voice"].configure(
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
            self._refresh_voice()
            self._job = self.root.after(100, self._follow)
        except tk.TclError:
            self.close()

    def close(self):
        self._closed = True
        if self._job is not None:
            self.root.after_cancel(self._job)
            self._job = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass
