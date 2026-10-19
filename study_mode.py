"""
Denisse -- study mode: a pomodoro timer that texts him from the orb.

25 minutes of study, 5 of rest, round and round until he stops it. Each
switch arrives the way the reminder card does (asked 2026-10-14, "similar
to texting like the reminder"): a "typing" bubble out of the orb, then a
short message -- "Rest time" or "Study time" -- with a Windows chime, so he
notices without looking. Same black bubble and animated orb border as the
reminder card; StudyBubble only swaps the task list for the message.

While it runs, a small pill under the orb counts down the current phase
(StudyBadge) -- click it for a status bubble.

Started and stopped from the tray menu, from the bubble itself, or by typing
or saying any of the phrases in match_command -- "study session", "start
pomodoro", "study session end", and while it is running the bare "turn it
off". No model call anywhere: it's a clock, it has to be exact, and it works
with Ollama down.

Timing runs on wall-clock deadlines checked every second rather than one
long Tk after(), so a laptop that sleeps mid-session doesn't come back and
fire a stale alert hours late as if nothing happened -- it notices the
deadline passed and moves on with a fresh phase from now.
"""

import re
import tkinter as tk
from datetime import datetime, timedelta

from PIL import Image, ImageColor, ImageDraw, ImageTk

from floating_orb import _flatten

from reminder_card import ACCENT, FAINT, FILL, GOOD, MUTED, TEXT, TRANSPARENT_KEY, ReminderCard, _work_area

STUDY_MIN = 25
REST_MIN = 5
CHECK_MS = 250  # also how quickly the badge catches up when the orb is dragged
BUBBLE_MS = 90_000  # the alert tidies itself away after 90s -- it's a ping, not a to-do
BUBBLE_W = 260

STUDY_LINES = (
    "Study time, Chuen.",
    "{n} minutes, starting now. Back to it.",
)
REST_LINES = (
    "Rest time.",
    "Nice work -- take {n} minutes. Stand up, drink some water.",
)


# What turns it on and off, typed or said. Whole-message only, so a question
# about studying still goes to the model.
#
# It only knew "study mode" in its first version, and on 2026-10-19
# learning.py found what Chuen actually typed: "study time", "pomodoro",
# "pamodoro", "off study mode". None matched, so each went to the model,
# which has no study-mode tool -- it guessed read_skill('study'), got "No
# skill named", and answered with a made-up pomodoro how-to. The fix is
# here rather than a new tool because this has to work with Ollama down.
_THING = (
    r"(?:the\s+|a\s+|my\s+)?"
    r"(?:study\s+(?:mode|time|timer|session)|p[ao]m[ao]doro(?:\s+timer)?)"
)
_STUDY_ON = re.compile(
    rf"^(?:(?:start|begin|turn\s+on|enter|open)\s+)?{_THING}(?:\s+on)?$"
    r"|^(?:time\s+to|let'?s|start)\s+study(?:ing)?$"
)
_STUDY_OFF = re.compile(
    rf"^(?:stop|end|exit|quit|turn\s+off|cancel|close|off)\s+{_THING}$"
    rf"|^{_THING}\s+(?:off|end|ends|over|done|finished)$"
    r"|^(?:i'?m\s+|i\s+am\s+|i'?ve\s+|i\s+have\s+|i\s+)?(?:done|finished|stop)\s+studying$"
)

# Phrases that name no subject, so they only mean the timer WHILE it is
# running. Asked for by Chuen on 2026-10-19: "turn the timer off and end it
# when I say 'turn it off'". That reverses the same day's earlier decision to
# leave "turn it off" to the model, and it is his call to reverse -- but the
# ambiguity it was protecting is real: with nothing running, "turn it off" is
# usually the music. So these resolve only when study mode is active, and
# otherwise fall through untouched.
#
# Known consequence, accepted: with music playing AND the timer running,
# "turn it off" stops the timer. The timer is the thing he just asked for;
# "turn off the music" still names its subject and goes to the model.
_STUDY_OFF_IF_ACTIVE = re.compile(
    r"^(?:turn|switch|shut)\s+(?:it|that|this)\s+off$"
    r"|^(?:stop|end|cancel|kill)\s+(?:it|that|this)$"
)

_CURRENT = None  # the one StudyMode in this process, so is_active() is real


def is_active() -> bool:
    """Is study mode running right now? False when there is no StudyMode."""
    return _CURRENT is not None and _CURRENT.active


def match_command(text: str, active=None):
    """-> 'study', 'stop_study', or None.

    `active` is whether study mode is running. Leave it None to read the live
    state (`is_active()`); pass a bool to decide explicitly, which is what the
    tests do. It only affects the subject-less phrases above -- everything
    else matches the same either way.
    """
    t = " ".join((text or "").lower().replace(",", " ").rstrip(".!?").split())
    t = re.sub(r"^(?:please\s+)|(?:\s+please)$", "", t)
    if _STUDY_OFF.match(t):
        return "stop_study"
    if _STUDY_ON.match(t):
        return "study"
    if active is None:
        active = is_active()
    if active and _STUDY_OFF_IF_ACTIVE.match(t):
        return "stop_study"
    return None


def _chime():
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass  # sound is a nicety; the bubble is the alert


class StudyBubble(ReminderCard):
    """One study-mode message out of the orb. Reuses ReminderCard's whole
    bubble -- placement, typing intro, breathing border, follow-the-orb --
    and only replaces what's inside it."""

    def __init__(self, root, orb, phase, ends_at, round_no, on_stop, font_family="Segoe UI", on_close=None, status=False):
        self.phase, self.ends_at, self.round_no, self.on_stop = phase, ends_at, round_no, on_stop
        self.status = status  # "where am I" (badge click) rather than a phase-change alert
        super().__init__(root, orb, None, font_family=font_family, on_close=on_close)
        self.win.after(BUBBLE_MS, self._expire)

    def _expire(self):
        # Tk doesn't cancel an after() when the window goes, so check it's
        # still this bubble's window -- a second close() would run on_close
        # again and could forget a newer bubble.
        try:
            if self.win.winfo_exists():
                self.close()
        except tk.TclError:
            pass

    def render(self):
        if self.typing:
            return super().render()  # the "• • •" intro, then _stop_typing re-renders
        if self.body is not None:
            self.body.destroy()
        self.body = tk.Frame(self.canvas, bg=FILL)
        self._img_key = None
        inner_w = self._px(BUBBLE_W - 12 * 2)

        study = self.phase == "study"
        title, line = STUDY_LINES if study else REST_LINES
        mins = STUDY_MIN if study else REST_MIN
        if self.status:
            left = max(0, round((self.ends_at - datetime.now()).total_seconds() / 60))
            title = "Studying." if study else "On a break."
            line = f"{left} min left of this {'study block' if study else 'rest'}."
        head = tk.Frame(self.body, bg=FILL)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=FILL, fg=TEXT, font=self._f(11, "bold")).pack(side="left")
        self._link(head, "Dismiss", self.close, fg=FAINT, hover=MUTED, size=9).pack(side="right", pady=(self._px(3), 0))
        tk.Label(
            self.body, text=line.format(n=mins), bg=FILL, fg=MUTED, font=self._f(9),
            anchor="w", wraplength=inner_w, justify="left",
        ).pack(fill="x")
        nxt = "rest" if study else "study"
        tk.Label(
            self.body, text=f"Round {self.round_no} · {nxt} at {self.ends_at:%H:%M}",
            bg=FILL, fg=ACCENT, font=self._f(8, "bold"), anchor="w",
        ).pack(fill="x", pady=(self._px(4), 0))
        foot = tk.Frame(self.body, bg=FILL)
        foot.pack(fill="x", pady=(self._px(4), 0))
        self._link(foot, "Stop study mode", self._stop, fg=FAINT, hover=MUTED, size=8).pack(side="left")
        tk.Label(foot, text=f"{datetime.now():%H:%M}", bg=FILL, fg=FAINT, font=self._f(7)).pack(side="right")
        self._layout(BUBBLE_W, inner_w)

    def refresh(self):
        self.win.lift()

    def _stop(self):
        self.close()
        self.on_stop()


BADGE_W, BADGE_H = 96, 24  # logical px
BADGE_GAP = 4  # clear space between the pill and the orb's ring
BADGE_TUCK = 5  # how far inside the orb window its ring's outer edge sits (GLOW_PAD - RING_GAP - stroke)


class StudyBadge:
    """The live countdown while study mode is on (asked 2026-10-14: "show
    the status of the timer and whether or not it's active"). A small pill
    under the orb -- above it when the orb sits on the bottom edge --
    reading "Study 18:24" or "Rest 3:12". Lavender for study, green for
    rest. It exists only while study mode is on, so "is it active" is just
    whether it's there. Click it for a status bubble.

    No progress bar: it was there until 2026-10-15, but early in a block
    it is a short stub near the left edge and reads as a stray dash in the
    pill, which is how Chuen saw it. The countdown already says how far in
    the block is.

    Its own small window rather than drawn on the orb: the orb's canvas is
    sized to the orb, and this way floating_orb.py doesn't change.
    """

    def __init__(self, root, orb, on_click, font_family="Segoe UI", avoid=None):
        self.root, self.orb, self.on_click = root, orb, on_click
        self.avoid = avoid or (lambda: [])  # windows it mustn't sit on (the bubbles beside the orb)
        try:
            self.s = root.winfo_fpixels("1i") / 96.0
        except Exception:
            self.s = 1.0
        self.w, self.h = round(BADGE_W * self.s), round(BADGE_H * self.s)
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            pass
        self.win.config(bg=TRANSPARENT_KEY)
        self.canvas = tk.Canvas(self.win, width=self.w, height=self.h, bg=TRANSPARENT_KEY, highlightthickness=0, bd=0, cursor="hand2")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", lambda e: self.on_click())
        self._pills = {}
        self._color = None
        self._img_id = self.canvas.create_image(0, 0, anchor="nw")
        # Centred, not nudged up: there is no bar under it any more.
        self._text = self.canvas.create_text(self.w // 2, self.h // 2, fill=TEXT, font=(font_family, 9, "bold"))

    def _pill(self, color):
        """Rounded pill, supersampled then snapped to the colour key --
        -transparentcolor is all-or-nothing, so edges can't be half alpha."""
        if color not in self._pills:
            k = 4
            big = Image.new("RGBA", (self.w * k, self.h * k), (0, 0, 0, 0))
            d = ImageDraw.Draw(big)
            d.rounded_rectangle((0, 0, self.w * k - 1, self.h * k - 1), radius=self.h * k // 2,
                                fill=FILL, outline=color, width=round(1.5 * self.s * k))
            small = big.resize((self.w, self.h), Image.LANCZOS)
            # Edge pixels blended onto the outline colour, not black -- a
            # plain convert("RGB") darkened the rim into dark specks on
            # white windows (2026-10-14).
            self._pills[color] = ImageTk.PhotoImage(_flatten(small, matte=ImageColor.getrgb(color), cut=128))
        return self._pills[color]

    def update(self, phase, ends_at, total_s):
        """Redraw for this second and keep it beside the orb."""
        try:
            left = max(0, (ends_at - datetime.now()).total_seconds())
            color = ACCENT if phase == "study" else GOOD
            if color != self._color:
                self._color = color
                self.canvas.itemconfig(self._img_id, image=self._pill(color))
            m, sec = divmod(int(left + 0.999), 60)
            self.canvas.itemconfig(self._text, text=f"{'Study' if phase == 'study' else 'Rest'}  {m}:{sec:02d}")
            self._follow()
        except tk.TclError:
            pass

    def _follow(self):
        try:
            ox, oy, o = self.orb.win.winfo_x(), self.orb.win.winfo_y(), self.orb.total_px
        except Exception:
            ox, oy, o = None, None, 0
        area = _work_area(self.win.winfo_screenwidth(), self.win.winfo_screenheight())
        if ox is None:
            x, y = area[2] - self.w - 12, area[3] - self.h - 12
        else:
            x = ox + o // 2 - self.w // 2
            # Tuck into the orb's glow pad as far as its ring, then leave the
            # same gap above or below so it lines up with the ring, not on it
            # (asked 2026-10-14: it sat on the ring when above the orb).
            tuck, gap = round(BADGE_TUCK * self.s), round(BADGE_GAP * self.s)
            y = oy + o - tuck + gap
            above = y + self.h > area[3]
            if above:
                y = oy - self.h + tuck - gap
            x = max(area[0], min(x, area[2] - self.w))
            # A bubble beside the orb is taller than the orb, so the pill
            # would land on its corner -- hop over it instead (seen in the
            # 2026-10-14 test screenshots).
            gap = round(BADGE_GAP * self.s)
            for win in self.avoid():
                try:
                    bx, by = win.winfo_rootx(), win.winfo_rooty()
                    bw, bh = win.winfo_width(), win.winfo_height()
                except tk.TclError:
                    continue
                if x < bx + bw and bx < x + self.w and y < by + bh and by < y + self.h:
                    y = by - self.h - gap if above else by + bh + gap
            y = max(area[1], min(y, area[3] - self.h))
        # Compared with where the window actually is, not a cached value --
        # a cache let a move get skipped and leave it on a bubble.
        geo = f"{self.w}x{self.h}+{x}+{y}"
        if self.win.geometry() != geo:
            self.win.geometry(geo)
        self.win.lift()

    def close(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class StudyMode:
    """The pomodoro clock. Main (Tk) thread only; tray.py marshals calls
    from other threads through root.after.

    show(phase, ends_at, round_no, status=False) is tray.py's hook for putting a message
    beside the orb -- it owns which bubble is up, so this class doesn't
    need to know about the reminder card or the thinking bubble.
    """

    def __init__(self, root, show, orb=None, font_family="Segoe UI", avoid=None):
        self.root = root
        self.show = show
        self.orb, self.font, self.avoid = orb, font_family, avoid
        self.badge = None
        self.phase = None  # "study", "rest", or None when off
        self.ends_at = None
        self.round_no = 0
        self._job = None
        # One StudyMode per process (tray.py builds it once). Registering it
        # here is what lets match_command answer "is it running?" without the
        # chat and the wake word each having to be handed the instance.
        global _CURRENT
        _CURRENT = self

    @property
    def active(self):
        return self.phase is not None

    def start(self):
        if self.active:
            self.show_status()  # already on: just say where we are
            return
        self.round_no = 0
        self._enter("study")
        try:
            self.badge = StudyBadge(self.root, self.orb, self.show_status, font_family=self.font, avoid=self.avoid)
        except Exception:
            self.badge = None  # cosmetic -- the clock and alerts still run
        self._tick_badge()
        self._job = self.root.after(CHECK_MS, self._check)

    def show_status(self):
        if self.active:
            self.show(self.phase, self.ends_at, self.round_no, status=True)

    def stop(self):
        self.phase = self.ends_at = None
        if self.badge is not None:
            self.badge.close()
            self.badge = None
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def status(self):
        """'Studying · 18 min left', or '' when off -- for the tray menu."""
        if not self.active:
            return ""
        left = max(0, round((self.ends_at - datetime.now()).total_seconds() / 60))
        return f"{'Studying' if self.phase == 'study' else 'Resting'} · {left} min left"

    def _enter(self, phase):
        self.phase = phase
        if phase == "study":
            self.round_no += 1
        self.ends_at = datetime.now() + timedelta(minutes=STUDY_MIN if phase == "study" else REST_MIN)
        _chime()
        self.show(phase, self.ends_at, self.round_no)

    def _check(self):
        self._job = None
        if not self.active:
            return
        if datetime.now() >= self.ends_at:
            # After a sleep this may be long overdue; either way the next
            # phase starts from now, never a backlog of alerts.
            self._enter("rest" if self.phase == "study" else "study")
        self._tick_badge()
        self._job = self.root.after(CHECK_MS, self._check)

    def _tick_badge(self):
        if self.badge is not None:
            total = (STUDY_MIN if self.phase == "study" else REST_MIN) * 60
            self.badge.update(self.phase, self.ends_at, total)
