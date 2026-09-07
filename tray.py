"""
Denisse -- system tray icon.

Puts a persistent icon in the Windows system tray (next to the clock, in
the "hidden icons" overflow at first -- drag it out onto the taskbar via
the ^ arrow if you want it always visible) for quick access:

    - Left-click / double-click: opens a small chat window right above the
      tray -- shows online/offline status and lets you type straight to
      Denisse right there, no console/terminal window involved
    - Right-click: a menu with the same chat action, a console-based voice
      mode for anyone who wants that instead, Mission Control,
      auto-organize toggle, project folder, and Quit

Also runs auto_organize.py's watch loop in a background thread (on by
default) so Denisse tidies your Downloads folder the whole time the tray
icon is up -- "Pause auto-organize" in the popup (or menu) turns it off.

Run:
    python tray.py

For it to start automatically on login, see README's "Autostart" section.
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
import webbrowser
from datetime import datetime
from tkinter import messagebox

import pystray
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

# Tk apps aren't DPI-aware by default, so on a scaled display (125%/150%/
# 200%...) Windows bitmap-stretches the whole window to compensate -- which
# blurs everything, including otherwise-crisp anti-aliased bubble images.
# This has to run before any window (Tk's or pystray's) is created, so it's
# at import time rather than inside main().
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import auto_organize
import conversation
import memory_store
import news
import obsidian_sync
import optimize
import wake_word
from floating_orb import FloatingOrb
from brain import Brain
from mission_control import get_status
from safety import RISKY_TOOLS
from voice_output import synthesize_inworld, prepare_audio, warm_up_audio

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Always launch console children (voice mode, Mission Control) with the real
# python.exe, never pythonw.exe -- when the tray itself autostarts via
# pythonw.exe (see README's Autostart section), sys.executable is pythonw,
# and a pythonw child has no usable stdin even in its own new console
# (verified: input() raises "lost sys.stdin"), which would silently break
# voice mode's input() prompt.
_exe_dir = os.path.dirname(sys.executable)
PYTHON_EXE = os.path.join(_exe_dir, "python.exe")
if not os.path.exists(PYTHON_EXE):
    PYTHON_EXE = sys.executable

DASHBOARD_URL = "http://127.0.0.1:8765"

ACCENT = (168, 85, 247)  # matches Mission Control's purple accent
BG = (10, 7, 16)  # matches Mission Control's black background

# Popup palette -- Tron-Legacy-inspired: pure black void, thin glowing
# purple line-work on angular (chamfered, not rounded) panels, rather than
# filled color blocks -- as hex strings since tkinter wants those rather
# than RGB tuples.
POPUP_BG = "#050308"  # near-pure black -- the "Grid" void
POPUP_BORDER = "#4c3575"  # dim glow-line purple, used for resting/inactive outlines
POPUP_TEXT = "#ffffff"
POPUP_MUTED = "#a99bc4"
STATUS_OK = "#3ddc84"
STATUS_WARN = "#e8b93b"
STATUS_BAD = "#e2555a"
ACCENT_HEX = "#%02x%02x%02x" % ACCENT
ACCENT_BRIGHT = "#c9a3ff"  # brightest glow-line purple, for active/outgoing outlines

# No filled header bar or filled chat bubbles -- Tron panels are dark glass
# with a thin glowing outline, not solid color blocks. HEADER_BG matches the
# body background so the header blends in and reads as one continuous black
# panel, separated only by a thin glow line (see ChatPopup.__init__).
HEADER_BG = POPUP_BG
BUBBLE_OUT_OUTLINE = ACCENT_BRIGHT  # your messages -- bright glowing outline
BUBBLE_OUT_FILL = (*ACCENT, 40)  # faint purple tint over black, not a filled block
BUBBLE_OUT_FG = "#ffffff"
BUBBLE_IN_OUTLINE = "#4c3575"  # Denisse's messages -- dim outline, no glow (visual hierarchy vs. yours)
BUBBLE_IN_FILL = (58, 42, 92, 26)  # barely-there tint, mostly just the void showing through
BUBBLE_IN_FG = POPUP_TEXT
BUBBLE_CHAMFER = 9  # corner cut size (px) -- angular sci-fi panel, not a rounded pill

# Solid fill colors, for the few widgets that can't do PIL alpha compositing
# (plain tk.Entry can't render a polygon outline+glow like the bubbles).
PANEL_BG = "#0d0916"  # slightly lighter than POPUP_BG so it still reads as a distinct surface
DISC_FILL = "#6d28d9"  # send button's solid glowing disc -- identity-disc purple
BUBBLE_MAX_TEXT_W = 165  # px of wrapped text before a bubble wraps a line --
# wider than a typical phone chat bubble on purpose: at the old 124px, most
# replies wrapped into many short lines, which read as a wall of text for
# something this narrow overall (see CHAT_WIDTH).
BUBBLE_PAD_X, BUBBLE_PAD_Y = 9, 6
BUBBLE_GAP = 5
SEND_BTN_SIZE = 30

# Preferred font stack, most modern-looking first -- resolved to whichever
# is actually installed the first time a ChatPopup is built (querying
# available fonts needs a live Tk root, so this can't run at import time).
_FONT_STACK = ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", "Calibri", "Arial")
FONT_FAMILY = "Segoe UI"  # safe default, overwritten by _resolve_font_family()


def _resolve_font_family():
    global FONT_FAMILY
    available = set(tkfont.families())
    for name in _FONT_STACK:
        if name in available:
            FONT_FAMILY = name
            return
    FONT_FAMILY = "TkDefaultFont"


# Raw pixel constants above (bubble radius, padding, window size, ...) are
# sized for a 96-DPI (100%-scale) screen. Now that the process is DPI-aware
# (see SetProcessDpiAwareness above), Tk stops relying on Windows to scale
# the window for us, so those pixel values need scaling by hand to still
# match the display's actual DPI -- _px() does that everywhere they're used.
DPI_SCALE = 1.0


def _resolve_dpi_scale():
    global DPI_SCALE
    try:
        DPI_SCALE = root.winfo_fpixels("1i") / 96.0
    except Exception:
        DPI_SCALE = 1.0


def _px(n):
    return round(n * DPI_SCALE)


def _hex_to_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _chamfered_points(w: int, h: int, c: int) -> list:
    """Corner points for an octagon -- a rectangle with its four corners cut
    at 45 degrees -- the angular sci-fi-panel shape Tron Legacy's UI uses in
    place of rounded corners."""
    c = min(c, w // 2, h // 2)
    return [
        (c, 0), (w - c, 0), (w, c), (w, h - c),
        (w - c, h), (c, h), (0, h - c), (0, c),
    ]


def _panel_image(
    w: int, h: int, chamfer: int, fill_rgba: tuple, outline_rgb: tuple, outline_w: int = 1, glow: bool = False
) -> ImageTk.PhotoImage:
    """A dark-glass Tron-style panel: chamfered (not rounded) corners, a
    barely-there tinted fill so the black void still shows through, and a
    crisp outline -- optionally with a soft glow bled out from behind it.
    Drawn at 4x scale then downsampled for anti-aliasing, same reasoning as
    the old rounded-rect helper this replaces."""
    scale = 4
    w, h = max(w, 1), max(h, 1)
    pts = _chamfered_points(w * scale, h * scale, chamfer * scale)

    img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    if glow:
        glow_layer = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
        ImageDraw.Draw(glow_layer).polygon(pts, outline=(*outline_rgb, 200), width=outline_w * scale * 3)
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=outline_w * scale * 1.5))
        img = Image.alpha_composite(img, glow_layer)

    draw = ImageDraw.Draw(img)
    draw.polygon(pts, fill=fill_rgba)
    draw.polygon(pts, outline=(*outline_rgb, 255), width=outline_w * scale)

    img = img.resize((w, h), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


def _glow_line_image(w: int, rgb: tuple, thickness: int = 1, glow_pad: int = 3) -> ImageTk.PhotoImage:
    """A thin horizontal glowing line -- stands in for a filled bar as a
    boundary marker (e.g. under the chat popup's header)."""
    scale = 4
    h = thickness + glow_pad * 2
    img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    cy = h * scale // 2
    glow_layer = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    ImageDraw.Draw(glow_layer).line([(0, cy), (w * scale, cy)], fill=(*rgb, 200), width=thickness * scale * 3)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=glow_pad * scale * 0.6))
    img = Image.alpha_composite(img, glow_layer)
    ImageDraw.Draw(img).line([(0, cy), (w * scale, cy)], fill=(*rgb, 255), width=thickness * scale)
    img = img.resize((w, h), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


def _circle_image(d: int, rgb: tuple, glow: bool = False) -> tuple:
    """A filled circle, optionally with a soft blurred glow halo behind it
    (a restrained neon touch -- used for the send button's active state)."""
    scale = 4
    pad = max(3, d // 4) if glow else 0  # modest room for the blur to bleed into -- kept small deliberately, restrained not neon-billboard
    size = d + pad * 2
    img = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if glow:
        glow_layer = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
        ImageDraw.Draw(glow_layer).ellipse(
            (pad * scale, pad * scale, (pad + d) * scale, (pad + d) * scale), fill=(*rgb, 130)
        )
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=d * scale * 0.12))
        img = Image.alpha_composite(img, glow_layer)
        draw = ImageDraw.Draw(img)
    draw.ellipse((pad * scale, pad * scale, (pad + d) * scale, (pad + d) * scale), fill=(*rgb, 255))
    img = img.resize((size, size), Image.LANCZOS)
    return ImageTk.PhotoImage(img), pad


def _side_glow_image(w: int, h: int, rgb: tuple) -> ImageTk.PhotoImage:
    """A soft vertical glow blob, bright in the middle and fading at both
    ends -- the traveling 'light worm' segment run up/down the chat popup's
    left/right edges."""
    scale = 4
    img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    cx = w * scale // 2
    glow_layer = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    ImageDraw.Draw(glow_layer).line(
        [(cx, h * scale * 0.2), (cx, h * scale * 0.8)], fill=(*rgb, 220), width=w * scale
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=h * scale * 0.1))
    img = Image.alpha_composite(img, glow_layer)
    img = img.resize((w, h), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


def _make_icon_image(size: int = 64) -> Image.Image:
    """Draw a simple 'D' badge so Denisse doesn't need a shipped icon asset."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 3, size - 3), fill=BG, outline=ACCENT, width=3)

    text = "D"
    font = None
    for font_name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            font = ImageFont.truetype(font_name, int(size * 0.55))
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]), text, fill=ACCENT, font=font)
    return img


def _open_voice_console(icon, item):
    """Legacy console-based voice mode, for anyone who wants push-to-talk
    instead of the in-popup text chat. Opens a real terminal window."""
    subprocess.Popen(
        [PYTHON_EXE, os.path.join(BASE_DIR, "voice_mode.py")],
        cwd=BASE_DIR,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def _open_mission_control(icon, item):
    try:
        urllib.request.urlopen(DASHBOARD_URL, timeout=1)
        webbrowser.open(DASHBOARD_URL)  # already running, just open a tab
    except Exception:
        subprocess.Popen(
            [PYTHON_EXE, os.path.join(BASE_DIR, "mission_control.py")],
            cwd=BASE_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )


def _open_project_folder(icon, item):
    os.startfile(BASE_DIR)


_tray_icon = None  # set in main() once the pystray Icon exists -- lets
# the optimize loop below show notifications via _tray_icon.notify(...)
_floating_orb = None  # set in main() once the FloatingOrb exists

_organize_thread = None
_organize_stop_event = threading.Event()


def _organize_loop():
    while not _organize_stop_event.is_set():
        for folder in auto_organize.WATCH_FOLDERS:
            auto_organize.organize_folder(folder)
        _organize_stop_event.wait(auto_organize.POLL_SECONDS)


def _is_organizing() -> bool:
    return _organize_thread is not None and _organize_thread.is_alive()


def _start_organizing():
    global _organize_thread
    if _is_organizing():
        return
    _organize_stop_event.clear()
    _organize_thread = threading.Thread(target=_organize_loop, daemon=True)
    _organize_thread.start()


def _stop_organizing():
    _organize_stop_event.set()


def _toggle_organize(icon, item):
    if _is_organizing():
        _stop_organizing()
    else:
        _start_organizing()


_optimize_thread = None
_optimize_stop_event = threading.Event()


def _optimize_loop():
    last_cleanup = 0.0
    while not _optimize_stop_event.is_set():
        now = time.time()
        if now - last_cleanup >= optimize.TEMP_POLL_SECONDS:
            optimize.clean_temp_files()
            last_cleanup = now
        for warning in optimize.check_resource_warnings():
            if _tray_icon is not None:
                try:
                    _tray_icon.notify(warning, "Denisse")
                except Exception:
                    pass  # notifications are best-effort, never fatal
        _optimize_stop_event.wait(optimize.RESOURCE_POLL_SECONDS)


def _is_optimizing() -> bool:
    return _optimize_thread is not None and _optimize_thread.is_alive()


def _start_optimizing():
    global _optimize_thread
    if _is_optimizing():
        return
    _optimize_stop_event.clear()
    _optimize_thread = threading.Thread(target=_optimize_loop, daemon=True)
    _optimize_thread.start()


def _stop_optimizing():
    _optimize_stop_event.set()


def _toggle_optimize(icon, item):
    if _is_optimizing():
        _stop_optimizing()
    else:
        _start_optimizing()


_wake_word_thread = None
_wake_word_stop_event = threading.Event()


def _on_wake_word_exchange(command: str, reply: str):
    if _tray_icon is None:
        return

    def _truncate(s, n=90):
        return s if len(s) <= n else s[: n - 1] + "…"

    try:
        _tray_icon.notify(_truncate(reply), f'"hey Denisse" — {_truncate(command, 40)}')
    except Exception:
        pass  # notifications are best-effort, never fatal


def _on_wake_word_heard(text: str):
    # Called from wake_word.py's background thread -- hop to the main
    # thread before touching any Tk widget. Shows in whichever of the chat
    # popup / floating orb happen to be around; a no-op if neither is
    # (nothing extra recorded either way -- same transcript data either path).
    def _do():
        if _chat_popup is not None:
            _chat_popup.show_heard(text)
        if _floating_orb is not None:
            _floating_orb.say(text)

    root.after(0, _do)


def _on_wake_word_mode_command(mode: str):
    if mode == "offline":
        _go_offline()
    else:
        _go_online()


def _on_wake_word_thinking(thinking: bool):
    # Called from wake_word.py's background thread -- hop to the main
    # thread before touching the orb's Tk widgets.
    def _do():
        if _floating_orb is not None:
            if thinking:
                _floating_orb.start_thinking()
            else:
                _floating_orb.stop_thinking()

    root.after(0, _do)


def _wake_word_loop():
    wake_word.run(
        stop_event=_wake_word_stop_event,
        on_exchange=_on_wake_word_exchange,
        on_heard=_on_wake_word_heard,
        on_thinking=_on_wake_word_thinking,
        is_offline=_is_manually_offline,
        on_mode_command=_on_wake_word_mode_command,
    )


def _is_listening_for_wake_word() -> bool:
    return _wake_word_thread is not None and _wake_word_thread.is_alive()


def _start_wake_word_listening():
    global _wake_word_thread
    if _is_listening_for_wake_word():
        return
    _wake_word_stop_event.clear()
    _wake_word_thread = threading.Thread(target=_wake_word_loop, daemon=True)
    _wake_word_thread.start()


def _stop_wake_word_listening():
    _wake_word_stop_event.set()


def _toggle_wake_word(icon, item):
    if _is_listening_for_wake_word():
        _stop_wake_word_listening()
    else:
        _start_wake_word_listening()


# Manual offline mode -- lets you pause Denisse's reply-processing on
# demand, triggered only by saying or typing the exact phrase "offline
# mode" (and back with "online mode"), never by clicking anything. Separate
# from whether Ollama itself is actually reachable (that's still what
# _status_display's connected/model_ready checks reflect -- this just
# forces the display to "Offline" and blocks new activity on top of that).
# The wake-word listener keeps running while offline -- it has to, in
# order to still catch "online mode" being said -- it just discards
# everything else it hears in the meantime (see wake_word.run's is_offline
# param); same idea for typed chat (see ChatPopup._send).
_manual_offline = threading.Event()

_MODE_COMMAND_RE = {"offline mode": "offline", "online mode": "online"}


def _match_mode_command(text: str):
    """-> 'offline', 'online', or None, matching wake_word.py's existing
    "exit"/"quit"/"stop listening" convention: the phrase, and nothing
    else, modulo case/surrounding whitespace/trailing punctuation."""
    return _MODE_COMMAND_RE.get(text.strip().lower().rstrip(".!"))


def _is_manually_offline() -> bool:
    return _manual_offline.is_set()


def _go_offline():
    if _manual_offline.is_set():
        return
    _manual_offline.set()
    _refresh_chat_popup_status()


def _go_online():
    if not _manual_offline.is_set():
        return
    _manual_offline.clear()
    _refresh_chat_popup_status()


def _refresh_chat_popup_status():
    if _chat_popup is not None:
        root.after(0, _chat_popup.refresh_status)


NEWS_POLL_SECONDS = 30 * 60  # check every 30 min whether today's briefing is due

_news_thread = None
_news_stop_event = threading.Event()


def _decline_risky_unattended(name, args):
    """No human is present to answer a confirmation dialog for this
    background-triggered brain call (the daily news briefing) -- auto-
    decline anything RISKY_TOOLS would otherwise ask about, rather than
    either hanging forever waiting for input or running it unsupervised."""
    return name not in RISKY_TOOLS


def _news_briefing_loop():
    while not _news_stop_event.is_set():
        if news.is_daily_briefing_due():
            try:
                brain = conversation.get_brain()
                messages = conversation.replay()
                turn_start = len(messages)
                messages.append({"role": "user", "content": "Give me a short summary of today's news."})
                reply, messages = brain.respond(messages, confirm=_decline_risky_unattended)
                conversation.record(messages[turn_start:])
                news.mark_daily_briefing_done()

                # The briefing used to also write a dated note into the
                # vault's News/ folder. Removed 2026-09-06 at Chuen's
                # request: he never went back and read them, and a new
                # file every day was quietly accumulating clutter (each
                # one an unlinked node in Obsidian's graph). The briefing
                # itself still runs and still notifies.

                if _tray_icon is not None:
                    try:
                        _tray_icon.notify(reply[:220], "Denisse — today's news")
                    except Exception:
                        pass  # notifications are best-effort, never fatal
            except Exception:
                pass  # a failed briefing (offline, Ollama down, ...) shouldn't crash this loop
        _news_stop_event.wait(NEWS_POLL_SECONDS)


def _is_news_briefing_active() -> bool:
    return _news_thread is not None and _news_thread.is_alive()


def _start_news_briefing():
    global _news_thread
    if _is_news_briefing_active():
        return
    _news_stop_event.clear()
    _news_thread = threading.Thread(target=_news_briefing_loop, daemon=True)
    _news_thread.start()


def _stop_news_briefing():
    _news_stop_event.set()


def _toggle_news_briefing(icon, item):
    if _is_news_briefing_active():
        _stop_news_briefing()
    else:
        _start_news_briefing()


def _quit(icon, item):
    _stop_organizing()
    _stop_optimizing()
    _stop_wake_word_listening()
    _stop_news_briefing()
    icon.stop()
    root.after(0, root.quit)


def _status_display(status: dict) -> tuple:
    """-> (dot_color, message): just online/offline, no model name."""
    if _is_manually_offline():
        return POPUP_MUTED, "Offline"
    if not status.get("connected"):
        return STATUS_BAD, "Offline"
    if not status.get("model_ready"):
        return STATUS_WARN, "Online"
    return STATUS_OK, "Online"


CHAT_WIDTH, CHAT_HEIGHT = 240, 310  # ~50% smaller by area than the original 340x440

# How long the "🎤 heard: ..." live indicator stays up before clearing --
# was 2.5s (too quick to actually read), lengthened on request.
HEARD_DISPLAY_MS = int(os.environ.get("DENISSE_HEARD_DISPLAY_MS", "8000"))


class ChatPopup:
    """An in-tray chat window: shows online/offline status and lets you
    type straight to Denisse, right there -- no console window."""

    def __init__(self):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)  # no title bar -- a flyout, not a window
        self.win.attributes("-topmost", True)
        self.win.configure(bg=POPUP_BG, highlightbackground=POPUP_BORDER, highlightthickness=1)

        w, h = _px(CHAT_WIDTH), _px(CHAT_HEIGHT)
        taskbar_height = _px(48)  # approximate -- just needs to clear the taskbar
        margin = _px(10)
        x = self.win.winfo_screenwidth() - w - margin
        y = self.win.winfo_screenheight() - taskbar_height - h - margin
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.bind("<Escape>", lambda e: self.close())

        # -- header: name + online/offline status. No filled bar (Tron panels
        # are dark glass, not color blocks) -- just a glowing hairline
        # separator below (added after this block) marks the boundary. --
        header = tk.Frame(self.win, bg=HEADER_BG)
        header.pack(side="top", fill="x")
        name_col = tk.Frame(header, bg=HEADER_BG)
        name_col.pack(side="left", padx=_px(10), pady=_px(5), fill="x", expand=True)
        tk.Label(
            name_col, text="D E N I S S E", bg=HEADER_BG, fg=POPUP_TEXT, font=(FONT_FAMILY, 11, "bold")
        ).pack(anchor="w")
        # Status dot + text -- toggled by saying or typing "offline mode" /
        # "online mode" (see _match_mode_command), not by clicking anything.
        status_sub = tk.Frame(name_col, bg=HEADER_BG)
        status_sub.pack(anchor="w")
        self._status_dot = tk.Canvas(
            status_sub, width=_px(8), height=_px(8), bg=HEADER_BG, highlightthickness=0
        )
        self._status_dot_id = self._status_dot.create_oval(1, 1, _px(7), _px(7), fill="", outline="")
        self._status_dot.pack(side="left")
        self._status_label = tk.Label(
            status_sub, text="", bg=HEADER_BG, fg=POPUP_MUTED, font=(FONT_FAMILY, 8)
        )
        self._status_label.pack(side="left", padx=(_px(4), 0))
        tk.Button(
            header, text="✕", command=self.close, bg=HEADER_BG, fg=POPUP_MUTED,
            activebackground=HEADER_BG, activeforeground=POPUP_TEXT, relief="flat", bd=0,
            font=(FONT_FAMILY, 10), cursor="hand2",
        ).pack(side="right", padx=_px(10))

        # Thin glowing line standing in for the header's old filled bar --
        # the one place a Tron panel actually earns a solid glow, since it
        # marks a real boundary (header vs. chat body) rather than filling
        # a whole surface.
        self._header_sep_img = _glow_line_image(_px(CHAT_WIDTH), _hex_to_rgb(ACCENT_HEX))
        tk.Label(self.win, image=self._header_sep_img, bg=POPUP_BG, bd=0).pack(side="top")

        # -- bottom-anchored input row, packed BEFORE the chat canvas so it
        # always keeps its space regardless of how tall the message list gets --
        input_row = tk.Frame(self.win, bg=POPUP_BG)
        input_row.pack(side="bottom", fill="x", padx=_px(8), pady=(_px(3), _px(7)))
        self.entry = tk.Entry(
            input_row, bg=PANEL_BG, fg=POPUP_TEXT, insertbackground=POPUP_TEXT,
            relief="flat", font=(FONT_FAMILY, 9), state="disabled",
            # Dim glow-line purple at rest, brightens to the full accent on
            # focus -- same outline language as the chat panels.
            highlightthickness=1, highlightbackground=POPUP_BORDER, highlightcolor=ACCENT_HEX,
            # Tk's default disabled-state colors are a light system grey/white,
            # unrelated to bg/fg above -- set these too or the box flashes
            # white every time it's disabled (sending, before Denisse connects).
            disabledbackground=PANEL_BG, disabledforeground=POPUP_MUTED,
        )
        self.entry.pack(
            side="left", fill="x", expand=True, ipady=_px(3), ipadx=_px(8), padx=(0, _px(6))
        )
        self.entry.bind("<Return>", self._send)

        # Round send button -- a PIL-rendered circle (anti-aliased) with an
        # arrow glyph, swapped between an accent and a muted variant instead
        # of using a plain (square) tk.Button.
        btn_size = _px(SEND_BTN_SIZE)
        self._send_circle_on, glow_pad = _circle_image(btn_size, _hex_to_rgb(DISC_FILL), glow=True)
        self._send_circle_off, _ = _circle_image(btn_size, _hex_to_rgb(POPUP_BORDER))
        canvas_size = btn_size + glow_pad * 2
        self.send_btn = tk.Canvas(
            input_row, width=canvas_size, height=canvas_size, bg=POPUP_BG,
            highlightthickness=0, bd=0,
        )
        self.send_btn.pack(side="right")
        center = canvas_size // 2
        self._send_circle_id = self.send_btn.create_image(center, center, image=self._send_circle_off)
        self._send_arrow_id = self.send_btn.create_text(
            center, center, text="➤", fill=POPUP_MUTED, font=(FONT_FAMILY, 11)
        )
        self.send_btn.tag_bind(self._send_circle_id, "<Button-1>", self._send)
        self.send_btn.tag_bind(self._send_arrow_id, "<Button-1>", self._send)

        self.typing_label = tk.Label(
            self.win, text="", bg=POPUP_BG, fg=POPUP_MUTED, font=(FONT_FAMILY, 8, "italic"),
            justify="left", wraplength=_px(CHAT_WIDTH - 28),
        )
        self.typing_label.pack(side="bottom", fill="x", padx=_px(14))

        # -- chat area: a Canvas of hand-drawn rounded bubbles, no scrollbar --
        self.canvas = tk.Canvas(self.win, bg=POPUP_BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=_px(6), pady=(_px(4), _px(2)))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self._font = tkfont.Font(family=FONT_FAMILY, size=9)
        self._time_font = tkfont.Font(family=FONT_FAMILY, size=7)
        self._y = _px(6)
        self._bubble_images = []  # keep PhotoImage refs alive (Tk won't otherwise)

        # -- side glow: a soft purple "light worm" traveling up the left
        # edge and down the right edge, on a loop -- placed (not packed) so
        # it overlays the existing layout as a thin strip rather than
        # taking up its own space. --
        strip_w = _px(3)
        self._glow_strip_h = _px(70)
        self._side_glow_img = _side_glow_image(strip_w, self._glow_strip_h, _hex_to_rgb(ACCENT_HEX))
        self._glow_left = tk.Canvas(self.win, width=strip_w, bg=POPUP_BG, highlightthickness=0, bd=0)
        self._glow_left.place(x=0, y=0, width=strip_w, height=h)
        self._glow_left_id = self._glow_left.create_image(strip_w // 2, -self._glow_strip_h, image=self._side_glow_img)
        self._glow_right = tk.Canvas(self.win, width=strip_w, bg=POPUP_BG, highlightthickness=0, bd=0)
        self._glow_right.place(x=w - strip_w, y=0, width=strip_w, height=h)
        self._glow_right_id = self._glow_right.create_image(
            strip_w // 2, h + self._glow_strip_h, image=self._side_glow_img
        )
        self._glow_anim_job = None
        self._animate_side_glow(h)

        # Recent prose only, not the whole stored history -- see
        # conversation.py for why replaying raw tool traffic flattened
        # Denisse's replies.
        self.messages = conversation.replay()
        self.brain = None
        self.busy = False
        self._turn_start = len(self.messages)
        self._turn_tail = []
        self._heard_clear_job = None
        self.refresh_status()
        self._render_history()

        self._connecting_y = self._y
        self._add_system("Connecting to Denisse...", tag="connecting")
        threading.Thread(target=self._init_brain, daemon=True).start()

    # -- startup --

    def _init_brain(self):
        try:
            # Shared with the wake-word listener and the news briefing --
            # one Brain per process, not one per face.
            brain, err = conversation.get_brain(), None
        except RuntimeError as e:
            brain, err = None, str(e)
        root.after(0, lambda: self._brain_ready(brain, err))

    def _brain_ready(self, brain, err):
        self.brain = brain
        self.canvas.delete("connecting")
        self._y = self._connecting_y
        if err:
            self._add_system(err)
        else:
            self.entry.config(state="normal")
            self._set_send_enabled(True)
            self.entry.focus_set()

    def refresh_status(self):
        """Repaints the status dot/text -- called on init and whenever
        "offline mode"/"online mode" is said or typed (see
        _refresh_chat_popup_status). The entry stays enabled either way --
        it has to, so "online mode" can still be typed back in."""
        try:
            color, message = _status_display(get_status())
            self._status_dot.itemconfig(self._status_dot_id, fill=color, outline=color)
            self._status_label.config(text=message)
        except tk.TclError:
            pass  # popup closed out from under this async callback -- fine

    def _set_send_enabled(self, enabled: bool):
        self.send_btn.itemconfig(
            self._send_circle_id, image=self._send_circle_on if enabled else self._send_circle_off
        )
        self.send_btn.itemconfig(
            self._send_arrow_id, fill=BUBBLE_OUT_FG if enabled else POPUP_MUTED
        )
        self.send_btn.config(cursor="hand2" if enabled else "arrow")

    # -- bubble drawing --

    def _wrap_text(self, text: str, max_w: int) -> list:
        lines, cur = [], ""
        for word in text.split():
            trial = f"{cur} {word}".strip()
            if self._font.measure(trial) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
            while self._font.measure(cur) > max_w and len(cur) > 1:
                cut = len(cur)
                while cut > 1 and self._font.measure(cur[:cut]) > max_w:
                    cut -= 1
                lines.append(cur[:cut])
                cur = cur[cut:]
        if cur:
            lines.append(cur)
        return lines or [""]

    def _canvas_w(self) -> int:
        w = self.canvas.winfo_width()
        return w if w > 1 else _px(CHAT_WIDTH) - _px(24)

    def _autoscroll(self):
        self.canvas.config(scrollregion=(0, 0, self._canvas_w(), self._y))
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def _add_bubble(self, who: str, text: str):
        is_out = who == "you"
        pad_x, pad_y, gap, margin = _px(BUBBLE_PAD_X), _px(BUBBLE_PAD_Y), _px(BUBBLE_GAP), _px(6)
        lines = self._wrap_text(text, _px(BUBBLE_MAX_TEXT_W))
        line_h = self._font.metrics("linespace")
        ts = datetime.now().strftime("%H:%M")
        text_w = max(self._font.measure(l) for l in lines)
        ts_w = self._time_font.measure(ts)
        bw = max(text_w, ts_w) + 2 * pad_x
        bh = round(line_h * len(lines) + 2 * pad_y + self._time_font.metrics("linespace"))

        canvas_w = self._canvas_w()
        if is_out:
            x2, x1 = canvas_w - margin, canvas_w - margin - bw
        else:
            x1, x2 = margin, margin + bw
        y1 = self._y

        fg = BUBBLE_OUT_FG if is_out else BUBBLE_IN_FG
        # Outgoing panels glow (bright outline, visible tint); incoming ones
        # stay dim and glow-free -- a deliberate hierarchy so the whole
        # popup doesn't read as one uniform neon wall.
        bubble_img = _panel_image(
            round(bw), bh, BUBBLE_CHAMFER,
            fill_rgba=(BUBBLE_OUT_FILL if is_out else BUBBLE_IN_FILL),
            outline_rgb=_hex_to_rgb(BUBBLE_OUT_OUTLINE if is_out else BUBBLE_IN_OUTLINE),
            outline_w=max(1, _px(1)),
            glow=is_out,
        )
        self._bubble_images.append(bubble_img)  # avoid GC-ing the PhotoImage
        self.canvas.create_image(x1, y1, anchor="nw", image=bubble_img)

        ty = y1 + pad_y
        for line in lines:
            self.canvas.create_text(
                x1 + pad_x, ty, text=line, anchor="nw", fill=fg, font=self._font
            )
            ty += line_h
        self.canvas.create_text(
            x2 - pad_x, y1 + bh - _px(2), text=ts, anchor="se", fill=fg, font=self._time_font,
        )

        self._y = y1 + bh + gap
        self._autoscroll()

    def _add_system(self, text: str, tag: str = None):
        canvas_w = self._canvas_w()
        lines = self._wrap_text(text, canvas_w - _px(24))
        line_h = self._time_font.metrics("linespace")
        kwargs = {"tags": (tag,)} if tag else {}
        y = self._y
        for line in lines:
            self.canvas.create_text(
                canvas_w / 2, y, text=line, anchor="n", fill=POPUP_MUTED,
                font=self._time_font, **kwargs,
            )
            y += line_h
        self._y = y + _px(BUBBLE_GAP)
        self._autoscroll()

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _render_history(self):
        for m in self.messages:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role == "user" and content:
                self._add_bubble("you", content)
            elif role == "assistant" and content:
                self._add_bubble("denisse", content)

    # -- sending --

    def _send(self, event=None):
        text = self.entry.get().strip()
        if not text:
            return "break"

        # Mode commands work regardless of busy/brain state -- offline mode
        # in particular has to be reliable even if Ollama is unreachable or
        # a reply is still composing, not just when everything's healthy.
        mode = _match_mode_command(text)
        if mode == "offline":
            self.entry.delete(0, "end")
            _go_offline()
            self._add_system('Offline. Type or say "online mode" to resume.')
            return "break"
        if mode == "online":
            self.entry.delete(0, "end")
            _go_online()
            self._add_system("Back online.")
            return "break"

        if self.busy or self.brain is None:
            return "break"
        if _is_manually_offline():
            self.entry.delete(0, "end")
            self._add_system('Offline -- say or type "online mode" to resume.')
            return "break"

        self.entry.delete(0, "end")
        self._add_bubble("you", text)
        self._turn_start = len(self.messages)
        self._turn_tail = []
        self.messages.append({"role": "user", "content": text})

        self.busy = True
        self.entry.config(state="disabled")
        self._set_send_enabled(False)
        self.typing_label.config(text="Denisse is typing...")
        if _floating_orb is not None:
            _floating_orb.start_thinking()

        threading.Thread(target=self._respond_worker, daemon=True).start()
        return "break"

    def _respond_worker(self):
        try:
            # Everything from the user message on is this turn's tail, which
            # is what gets appended to the stored history in _respond_done.
            turn_start = self._turn_start
            reply, self.messages = self.brain.respond(self.messages, confirm=self._confirm)
            self._turn_tail = self.messages[turn_start:]
            err = None
        except Exception as e:
            reply, err = None, str(e)
        root.after(0, lambda: self._respond_done(reply, err))

    def _confirm(self, name: str, args: dict) -> bool:
        """Runs on the worker thread -- Tk isn't thread-safe, so the actual
        dialog is scheduled on the main thread and this blocks on an Event
        until it's answered."""
        if name not in RISKY_TOOLS:
            return True
        done = threading.Event()
        result = {}

        def ask():
            summary = ", ".join(f"{k}={v!r}" for k, v in args.items())
            result["answer"] = messagebox.askyesno(
                "Denisse wants to do something",
                f"Allow {name}({summary})?",
                parent=self.win,
            )
            done.set()

        root.after(0, ask)
        done.wait()
        return result.get("answer", False)

    def _respond_done(self, reply, err):
        try:
            if err:
                self.typing_label.config(text="")
                if _floating_orb is not None:
                    _floating_orb.stop_thinking()
                self._add_system(f"Error: {err}")
                self._finish_turn()
                return
        except tk.TclError:
            return  # popup closed out from under this async callback -- fine

        # Append just this turn, re-reading the file first, so a reply here
        # can't erase what the wake-word listener recorded while the popup
        # sat open (see conversation.record).
        conversation.record(self._turn_tail)
        # Synthesizing (network, ~1-2s) AND opening the audio file for
        # playback (real MCI overhead -- see voice_output.py) both happen
        # silently in the background; the text bubble is revealed and
        # playback fired at the same instant, so only the unavoidable final
        # "play" command's own latency remains between them. A no-op if
        # INWORLD_API_KEY isn't set -- reveal just happens almost instantly.
        threading.Thread(target=self._synthesize_then_reveal, args=(reply,), daemon=True).start()

    def _synthesize_then_reveal(self, reply: str):
        path = synthesize_inworld(reply)
        audio = prepare_audio(path) if path else None
        play_now = threading.Event() if audio is not None else None
        root.after(0, lambda: self._reveal_reply(reply, audio, play_now))
        if audio is not None:
            # Windows' MCI ties an opened device to the thread that opened
            # it -- calling .play()/.close() from a different thread than
            # prepare_audio() ran on fails with "device is not open". So
            # this same thread waits here (blocking only itself, not the
            # UI) until the bubble reveal fires, then plays and closes.
            play_now.wait()
            try:
                audio.play()
            finally:
                audio.close()

    def _reveal_reply(self, reply: str, audio, play_now):
        try:
            self.typing_label.config(text="")
            if _floating_orb is not None:
                _floating_orb.stop_thinking()
            if play_now is not None:
                play_now.set()  # let the waiting synth thread proceed to .play()
            self._add_bubble("denisse", reply)
            self._finish_turn()
        except tk.TclError:
            pass  # popup closed out from under this async callback -- fine
        finally:
            # Always release the synth thread even if the popup was gone
            # before we got to it above -- otherwise it hangs forever on
            # play_now.wait(). Event.set() is idempotent, so this is a
            # harmless no-op on the normal path where it already fired.
            if play_now is not None:
                play_now.set()

    def show_heard(self, text: str):
        """Live "the mic is picking you up" feedback for the wake-word
        listener -- shows every transcript it hears (matched or not, same
        data already used for wake-word matching, nothing new recorded)
        in the typing-indicator label, auto-clearing after
        HEARD_DISPLAY_MS. Skipped while actively composing a real reply,
        so it never clobbers "Denisse is typing..."."""
        if self.busy:
            return
        try:
            if self._heard_clear_job is not None:
                self.win.after_cancel(self._heard_clear_job)
            display = text if len(text) <= 60 else text[:59] + "…"
            self.typing_label.config(text=f'🎤 heard: "{display}"')
            self._heard_clear_job = self.win.after(HEARD_DISPLAY_MS, self._clear_heard)
        except tk.TclError:
            pass  # popup closed out from under this async callback -- fine

    def _clear_heard(self):
        self._heard_clear_job = None
        if not self.busy:
            try:
                self.typing_label.config(text="")
            except tk.TclError:
                pass

    def _finish_turn(self):
        self.busy = False
        self.entry.config(state="normal")
        self._set_send_enabled(True)
        self.entry.focus_set()

    # -- lifecycle --

    def _animate_side_glow(self, win_h: int):
        step = _px(4)
        try:
            x, y = self._glow_left.coords(self._glow_left_id)
            y += step
            if y > win_h + self._glow_strip_h:
                y = -self._glow_strip_h
            self._glow_left.coords(self._glow_left_id, x, y)

            x2, y2 = self._glow_right.coords(self._glow_right_id)
            y2 -= step
            if y2 < -self._glow_strip_h:
                y2 = win_h + self._glow_strip_h
            self._glow_right.coords(self._glow_right_id, x2, y2)
        except tk.TclError:
            return  # popup closed out from under this timer -- just stop
        self._glow_anim_job = self.win.after(40, lambda: self._animate_side_glow(win_h))

    def close(self):
        global _chat_popup
        if self._glow_anim_job is not None:
            try:
                self.win.after_cancel(self._glow_anim_job)
            except Exception:
                pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        _chat_popup = None

    def focus(self):
        self.win.lift()
        self.win.focus_force()
        self.entry.focus_set()


_chat_popup = None


def _open_chat(icon=None, item=None):
    def _do():
        global _chat_popup
        if _chat_popup is not None:
            _chat_popup.focus()
        else:
            _chat_popup = ChatPopup()

    root.after(0, _do)




def main():
    global root
    root = tk.Tk()
    root.withdraw()  # only the popup shows -- no persistent main window
    _resolve_font_family()
    _resolve_dpi_scale()
    # Absorb Windows' one-time audio-subsystem cold-start cost now, in the
    # background, instead of on the first real reply (see voice_output.py).
    threading.Thread(target=warm_up_audio, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("Talk to Denisse", _open_chat, default=True),
        pystray.MenuItem("Voice mode (console)", _open_voice_console),
        pystray.MenuItem("Mission Control", _open_mission_control),
        pystray.MenuItem(
            "Auto-organize Downloads", _toggle_organize, checked=lambda item: _is_organizing()
        ),
        pystray.MenuItem(
            "Auto-optimize (cleanup + monitoring)", _toggle_optimize, checked=lambda item: _is_optimizing()
        ),
        pystray.MenuItem(
            'Listen for "hey Denisse"', _toggle_wake_word, checked=lambda item: _is_listening_for_wake_word()
        ),
        pystray.MenuItem(
            "Daily CNA news briefing", _toggle_news_briefing, checked=lambda item: _is_news_briefing_active()
        ),
        pystray.MenuItem("Project folder", _open_project_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )
    global _tray_icon
    _tray_icon = pystray.Icon("denisse", _make_icon_image(), "Denisse", menu)
    _start_organizing()
    _start_optimizing()
    _start_news_briefing()

    global _floating_orb
    _floating_orb = FloatingOrb(root, on_activate=_open_chat)

    _tray_icon.run_detached()  # icon runs on its own thread; tkinter owns the main thread
    root.mainloop()


if __name__ == "__main__":
    main()
