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
off". That whole-message path makes no model call: it's a clock, it has to
be exact, and it works with Ollama down. The phrases are data
(tool_policy.json, "study_commands"), not literals here.

Since 2026-11-03 the clock is also a tool set the model can see and drive
(session_status / start / pause / resume / stop at the bottom of this file,
registered in tools.py), because the evening before it wasn't: "pause
sutuy timer" paused Spotify and said "Sutuy timer paused", "is the study
session running?" listed a made-up Documents\\study folder, and "session
over" got "No tool needed here". The model is told the live state in one
prompt line (context_line) and acts through the tools; the clock itself
stays exact and stays on the Tk thread -- tray.py registers a dispatcher
that marshals a tool call there and waits for the answer.

Timing runs on wall-clock deadlines checked every second rather than one
long Tk after(), so a laptop that sleeps mid-session doesn't come back and
fire a stale alert hours late as if nothing happened -- it notices the
deadline passed and moves on with a fresh phase from now.
"""

import os
import re
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageTk

import json_config
import tool_policy
from floating_orb import _flatten

from reminder_card import ACCENT, FAINT, FILL, GOOD, MUTED, TEXT, TRANSPARENT_KEY, ReminderCard, _work_area

# Timings, sizes, fonts and every word it shows or tells the model live in
# study_mode.json (AGENTS.md Rule 0), checked by json_config.py against
# study_mode.defaults.json; configure() reloads it. The defaults are 25
# minutes of study, 5 of rest, a 250ms deadline check (also how quickly the
# badge catches up when the orb is dragged) and a 90s alert -- it's a ping,
# not a to-do.
CONFIG_ENV_VAR = "DENISSE_STUDY_MODE_CONFIG"
CONFIG_FILE = Path(__file__).with_name("study_mode.json")
DEFAULTS_FILE = Path(__file__).with_name("study_mode.defaults.json")

CONFIG: dict = {}


def _extra_checks(data: dict, defaults: dict, checker) -> dict:
    try:
        re.compile(data["command_strip"])
    except re.error as e:
        data["command_strip"] = checker.bad("study_mode.command_strip", f"has a bad pattern ({e})",
                                            defaults["command_strip"])
    try:
        datetime(2000, 1, 1).strftime(data["time_format"])
    except (ValueError, TypeError) as e:
        data["time_format"] = checker.bad("study_mode.time_format", f"isn't a time format ({e})",
                                          defaults["time_format"])
    return data


def configure(path=None) -> dict:
    """(Re)load study_mode.json (or ``path``, or $DENISSE_STUDY_MODE_CONFIG)
    and rebind the module values from it. A running StudyMode picks the new
    timings up at its next phase."""
    global CONFIG, STUDY_MIN, REST_MIN, CHECK_MS, BUBBLE_MS, BUBBLE_W, _STRIP
    global BADGE_W, BADGE_H, BADGE_GAP, BADGE_TUCK
    path = path or os.environ.get(CONFIG_ENV_VAR) or CONFIG_FILE
    CONFIG = json_config.load("study_mode", path, DEFAULTS_FILE, extra=_extra_checks)
    t, s = CONFIG["timings"], CONFIG["sizes"]
    STUDY_MIN, REST_MIN = t["study_min"], t["rest_min"]
    CHECK_MS, BUBBLE_MS = t["check_ms"], t["bubble_ms"]
    BUBBLE_W = s["bubble_w"]
    BADGE_W, BADGE_H = s["badge_w"], s["badge_h"]
    BADGE_GAP, BADGE_TUCK = s["badge_gap"], s["badge_tuck"]
    _STRIP = re.compile(CONFIG["command_strip"])
    return CONFIG


def _w(key: str, **fields) -> str:
    """One line of study_mode.json "wording", filled in."""
    return CONFIG["wording"][key].format(**fields)


def _ph(phase: str) -> dict:
    return CONFIG["phases"]["study" if phase == "study" else "rest"]


def _clock(when) -> str:
    return f"{when:{CONFIG['time_format']}}"


def _minutes(phase: str) -> int:
    return STUDY_MIN if phase == "study" else REST_MIN


configure()


# What turns it on and off, typed or said. Whole-message only, so a question
# about studying still goes to the model. The patterns are
# tool_policy.json "study_commands": "thing" names the timer ("study mode",
# "pomodoro timer", "pamodoro"), "on"/"off" are the whole-message forms
# around it, and "off_if_active" the subject-less ones.
#
# It only knew "study mode" in its first version, and on 2026-10-19
# learning.py found what Chuen actually typed: "study time", "pomodoro",
# "pamodoro", "off study mode". None matched, so each went to the model,
# which then had no study-mode tool -- it guessed read_skill('study'), got
# "No skill named", and answered with a made-up pomodoro how-to. The model
# has the tools now (below); this path stays because it works with Ollama
# down and costs no model call.
#
# "off_if_active": phrases that name no subject, so they only mean the timer
# WHILE it is running. Asked for by Chuen on 2026-10-19: "turn the timer off
# and end it when I say 'turn it off'". That reverses the same day's earlier
# decision to leave "turn it off" to the model, and it is his call to
# reverse -- but the ambiguity it was protecting is real: with nothing
# running, "turn it off" is usually the music. So these resolve only when
# study mode is active, and otherwise fall through untouched.
#
# Known consequence, accepted: with music playing AND the timer running,
# "turn it off" stops the timer. The timer is the thing he just asked for;
# "turn off the music" still names its subject and goes to the model.
# The bare one-worders are there because he typed "off" at 23:08 on
# 2026-10-19 with the timer visibly running, and the first version of this
# rule left them out as "too broad". That was wrong: breadth is exactly what
# `active` buys. Bare "timer" is deliberately NOT there: "stop timer" means
# the set_timer countdown (decisions.md 2026-10-20), and with a 5-minute
# timer running as well, "cancel timer" means the timer, not the pomodoro.


def _patterns() -> tuple:
    sc = tool_policy.study_commands()
    thing = sc["thing"]
    return (re.compile(sc["on"].replace("{thing}", thing)),
            re.compile(sc["off"].replace("{thing}", thing)),
            re.compile(sc["off_if_active"]))


_STUDY_ON, _STUDY_OFF, _STUDY_OFF_IF_ACTIVE = _patterns()

_CURRENT = None  # the one StudyMode in this process, so is_active() is real
_dispatch = None  # tray.py: run a callable on the Tk thread and return its result


def set_dispatcher(fn) -> None:
    """How the tool functions below reach the clock from another thread.
    `fn(callable)` must run it where the StudyMode lives and return what it
    returns. None (the default, the eval, the CLI) calls directly."""
    global _dispatch
    _dispatch = fn


def _on_clock(fn):
    return _dispatch(fn) if _dispatch is not None else fn()


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
    t = _STRIP.sub("", t)
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
        inner_w = self._px(BUBBLE_W - CONFIG["sizes"]["bubble_pad"] * 2)
        fonts = CONFIG["fonts"]

        ph = _ph(self.phase)
        title, line = ph["alert_title"], ph["alert_line"].format(n=_minutes(self.phase))
        if self.status:
            left = max(0, round((self.ends_at - datetime.now()).total_seconds() / 60))
            title, line = ph["status_title"], ph["status_line"].format(left=left)
        head = tk.Frame(self.body, bg=FILL)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=FILL, fg=TEXT, font=self._f(fonts["title"], "bold")).pack(side="left")
        self._link(head, _w("dismiss"), self.close, fg=FAINT, hover=MUTED,
                   size=fonts["body"]).pack(side="right", pady=(self._px(3), 0))
        tk.Label(
            self.body, text=line, bg=FILL, fg=MUTED, font=self._f(fonts["body"]),
            anchor="w", wraplength=inner_w, justify="left",
        ).pack(fill="x")
        tk.Label(
            self.body, text=ph["footer"].format(round=self.round_no, time=_clock(self.ends_at)),
            bg=FILL, fg=ACCENT, font=self._f(fonts["footer"], "bold"), anchor="w",
        ).pack(fill="x", pady=(self._px(4), 0))
        foot = tk.Frame(self.body, bg=FILL)
        foot.pack(fill="x", pady=(self._px(4), 0))
        self._link(foot, _w("stop_link"), self._stop, fg=FAINT, hover=MUTED, size=fonts["footer"]).pack(side="left")
        tk.Label(foot, text=_clock(datetime.now()), bg=FILL, fg=FAINT, font=self._f(fonts["clock"])).pack(side="right")
        self._layout(BUBBLE_W, inner_w)

    def refresh(self):
        self.win.lift()

    def _stop(self):
        self.close()
        self.on_stop()


# BADGE_W/H, BADGE_GAP (clear space between the pill and the orb's ring) and
# BADGE_TUCK (how far inside the orb window its ring's outer edge sits:
# GLOW_PAD - RING_GAP - stroke) are logical px from study_mode.json "sizes".


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
        self._text = self.canvas.create_text(self.w // 2, self.h // 2, fill=TEXT,
                                             font=(font_family, CONFIG["fonts"]["badge"], "bold"))

    def _pill(self, color):
        """Rounded pill, supersampled then snapped to the colour key --
        -transparentcolor is all-or-nothing, so edges can't be half alpha."""
        if color not in self._pills:
            k = 4
            big = Image.new("RGBA", (self.w * k, self.h * k), (0, 0, 0, 0))
            d = ImageDraw.Draw(big)
            d.rounded_rectangle((0, 0, self.w * k - 1, self.h * k - 1), radius=self.h * k // 2,
                                fill=FILL, outline=color, width=round(CONFIG["sizes"]["badge_outline"] * self.s * k))
            small = big.resize((self.w, self.h), Image.LANCZOS)
            # Edge pixels blended onto the outline colour, not black -- a
            # plain convert("RGB") darkened the rim into dark specks on
            # white windows (2026-10-14).
            self._pills[color] = ImageTk.PhotoImage(_flatten(small, matte=ImageColor.getrgb(color), cut=128))
        return self._pills[color]

    def update(self, phase, ends_at, total_s, paused_left=None):
        """Redraw for this second and keep it beside the orb. `paused_left`
        (seconds) freezes the countdown and labels it Paused."""
        try:
            left = paused_left if paused_left is not None else max(0, (ends_at - datetime.now()).total_seconds())
            color = ACCENT if phase == "study" else GOOD
            if color != self._color:
                self._color = color
                self.canvas.itemconfig(self._img_id, image=self._pill(color))
            m, sec = divmod(int(left + 0.999), 60)
            label = _w("paused_badge") if paused_left is not None else _ph(phase)["badge"]
            self.canvas.itemconfig(self._text, text=_w("badge", label=label, m=m, s=sec))
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
            margin = CONFIG["sizes"]["screen_margin"]
            x, y = area[2] - self.w - margin, area[3] - self.h - margin
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
                    if not win.winfo_ismapped():
                        continue
                    bx, by = win.winfo_rootx(), win.winfo_rooty()
                    bw, bh = win.winfo_width(), win.winfo_height()
                except tk.TclError:
                    continue
                if x < bx + bw and bx < x + self.w and y < by + bh and by < y + self.h:
                    below_y = by + bh + gap
                    y = by - self.h - gap if above or below_y + self.h > area[3] else below_y
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

    def __init__(self, root, show, orb=None, font_family="Segoe UI", avoid=None, on_change=None):
        self.root = root
        self.show = show
        self.orb, self.font, self.avoid = orb, font_family, avoid
        self.on_change = on_change  # tray.py: called after start/stop/pause/resume
        self.badge = None
        self.phase = None  # "study", "rest", or None when off
        self.ends_at = None
        self.round_no = 0
        self.paused_left = None  # seconds left in the phase while paused, else None
        self._job = None
        # One StudyMode per process (tray.py builds it once). Registering it
        # here is what lets match_command answer "is it running?" without the
        # chat and the wake word each having to be handed the instance.
        global _CURRENT
        _CURRENT = self

    @property
    def active(self):
        return self.phase is not None

    @property
    def paused(self):
        return self.active and self.paused_left is not None

    def _changed(self):
        if self.on_change is not None:
            try:
                self.on_change(self)
            except Exception:
                pass  # the menu or a bubble; never the clock

    def start(self):
        if self.active:
            self.show_status()  # already on: just say where we are
            return
        self.round_no = 0
        self.paused_left = None
        self._enter("study")
        try:
            self.badge = StudyBadge(self.root, self.orb, self.show_status, font_family=self.font, avoid=self.avoid)
        except Exception:
            self.badge = None  # cosmetic -- the clock and alerts still run
        self._tick_badge()
        self._job = self.root.after(CHECK_MS, self._check)
        self._changed()

    def show_status(self):
        if self.active:
            self.show(self.phase, self.ends_at, self.round_no, status=True)

    def stop(self):
        self.phase = self.ends_at = None
        self.paused_left = None
        if self.badge is not None:
            self.badge.close()
            self.badge = None
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        self._changed()

    def pause(self):
        """Freeze the current phase where it is. The tick stops; resume()
        gives the phase its remaining time from then."""
        if not self.active or self.paused:
            return
        self.paused_left = max(0.0, (self.ends_at - datetime.now()).total_seconds())
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        self._tick_badge()
        self._changed()

    def resume(self):
        if not self.paused:
            return
        self.ends_at = datetime.now() + timedelta(seconds=self.paused_left)
        self.paused_left = None
        self._tick_badge()
        self._job = self.root.after(CHECK_MS, self._check)
        self._changed()

    def minutes_left(self) -> int:
        if not self.active:
            return 0
        secs = self.paused_left if self.paused else (self.ends_at - datetime.now()).total_seconds()
        return max(0, round(secs / 60))

    def status(self):
        """'Studying · 18 min left', 'Paused · 18 min left', or '' when off
        -- for the tray menu."""
        if not self.active:
            return ""
        word = _w("paused_menu") if self.paused else _ph(self.phase)["menu"]
        return _w("menu_status", word=word, left=self.minutes_left())

    def describe(self) -> str:
        """The clock's state in a sentence, for the model and its tools."""
        if not self.active:
            return _w("off")
        ph = _ph(self.phase)
        if self.paused:
            return _w("paused_state", left=self.minutes_left(), block=ph["block"], round=self.round_no)
        return _w("running_state", doing=ph["doing"], left=self.minutes_left(), block=ph["block"],
                  round=self.round_no, next=ph["next"], time=_clock(self.ends_at))

    def _enter(self, phase):
        self.phase = phase
        if phase == "study":
            self.round_no += 1
        self.ends_at = datetime.now() + timedelta(minutes=_minutes(phase))
        _chime()
        self.show(phase, self.ends_at, self.round_no)

    def _check(self):
        self._job = None
        if not self.active or self.paused:
            return
        if datetime.now() >= self.ends_at:
            # After a sleep this may be long overdue; either way the next
            # phase starts from now, never a backlog of alerts.
            self._enter("rest" if self.phase == "study" else "study")
        self._tick_badge()
        self._job = self.root.after(CHECK_MS, self._check)

    def _tick_badge(self):
        if self.badge is not None:
            total = _minutes(self.phase) * 60
            self.badge.update(self.phase, self.ends_at, total, paused_left=self.paused_left)


# ---- the model-visible tools (tools.py registers them) ----------------------
# Each returns what actually happened, in plain words, and never claims
# more: "nothing was paused" when nothing was running is the honest result,
# and brain._confirms_nothing reads it as no action taken.

def session_status() -> str:
    if _CURRENT is None:
        return _w("no_clock_status")
    return _on_clock(_CURRENT.describe)


def start_session() -> str:
    if _CURRENT is None:
        return _w("no_clock")

    def go():
        if _CURRENT.active:
            return _w("already_running", state=_CURRENT.describe())
        _CURRENT.start()
        return _w("started", study_min=STUDY_MIN, rest_min=REST_MIN, time=_clock(_CURRENT.ends_at))
    return _on_clock(go)


def pause_session() -> str:
    if _CURRENT is None:
        return _w("no_clock")

    def go():
        if not _CURRENT.active:
            return _w("not_running_pause")
        if _CURRENT.paused:
            return _w("already_paused", state=_CURRENT.describe())
        _CURRENT.pause()
        return _w("paused", left=_CURRENT.minutes_left())
    return _on_clock(go)


def resume_session() -> str:
    if _CURRENT is None:
        return _w("no_clock")

    def go():
        if not _CURRENT.active:
            return _w("not_running_resume")
        if not _CURRENT.paused:
            return _w("not_paused", state=_CURRENT.describe())
        _CURRENT.resume()
        return _w("resumed", left=_CURRENT.minutes_left(), time=_clock(_CURRENT.ends_at))
    return _on_clock(go)


def stop_session() -> str:
    if _CURRENT is None:
        return _w("no_clock")

    def go():
        if not _CURRENT.active:
            return _w("not_running_stop")
        rounds = _CURRENT.round_no
        _CURRENT.stop()
        return _w("stopped", rounds=rounds, round_word=_w("round_one" if rounds == 1 else "round_many"))
    return _on_clock(go)


def context_line() -> str:
    """One line for the system prompt while a session is running, so the
    model knows without a call; "" when off, so idle turns pay nothing."""
    if _CURRENT is None or not _CURRENT.active:
        return ""
    try:
        return _w("context", state=_CURRENT.describe())
    except Exception:
        return ""
