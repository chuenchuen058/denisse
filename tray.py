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
import re
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
import standing_instructions
import conversation
import memory_store
import news
import tasks
import obsidian_sync
import optimize
import wake_word
import speech_control
import voice_pipeline
import hotkey
import explain_hotkey
from floating_orb import FloatingOrb
from orb_controls import OrbControls
from reminder_card import TYPING_MS, ReminderCard, ThinkingBubble, _place, _work_area
# "study mode" / "pomodoro" / "stop study mode" ... -- the phrases live in
# study_mode.match_command, shared with the wake word.
import study_mode
from study_mode import StudyBubble, StudyMode, match_command as _match_study_command
from timer_ui import TimerDisplay

_timer_display = None
from brain import Brain
from mission_control import get_status
from safety import RISKY_TOOLS
from voice_output import synthesize_inworld, prepare_audio, warm_up_audio, speak_streaming

import json_config
import mission_control
import ui_theme

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# tray.json: commands, routes, wording, timings, sizes, fonts and colours
# (AGENTS.md Rule 0, 2026-11-04). The shipped values, key by key, are in
# tray.defaults.json; a bad or missing value logs a line and uses that.
CONFIG_FILE = os.path.join(BASE_DIR, "tray.json")
DEFAULTS_FILE = os.path.join(BASE_DIR, "tray.defaults.json")
CONFIG_ENV_VAR = "DENISSE_TRAY_CONFIG"
ROUTES = ("tray", "model")  # what commands.route.<name> may say -- names the code acts on
_COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
CONFIG = {}


def _extra_checks(data: dict, defaults: dict, checker) -> dict:
    cmds = data["commands"]
    for name, route in cmds["route"].items():
        if route not in ROUTES:
            cmds["route"][name] = checker.bad(f"tray.commands.route.{name}", f"should be one of {ROUTES}",
                                              defaults["commands"]["route"][name])
    for key in ("offline_phrases", "online_phrases"):
        if not [p for p in cmds[key] if p.strip()]:
            cmds[key] = checker.bad(f"tray.commands.{key}", "is empty", defaults["commands"][key])
    try:
        re.compile(cmds["reminders"])
    except re.error as e:
        cmds["reminders"] = checker.bad("tray.commands.reminders", f"doesn't compile ({e})",
                                        defaults["commands"]["reminders"])
    for key, value in data["colours"].items():
        if isinstance(value, str) and not _COLOUR_RE.match(value):
            data["colours"][key] = checker.bad(f"tray.colours.{key}", "should be #rrggbb", defaults["colours"][key])
        elif isinstance(value, int) and value > 255:
            data["colours"][key] = checker.bad(f"tray.colours.{key}", "should be 0-255", defaults["colours"][key])
    look = data["glow"]
    for key, value in look.items():
        if key.endswith("_alpha") and not 0 <= value <= 255:
            look[key] = checker.bad(f"tray.glow.{key}", "should be 0-255", defaults["glow"][key])
        elif value < 0:
            look[key] = checker.bad(f"tray.glow.{key}", "should not be negative", defaults["glow"][key])
    if not 0 <= look["worm_start"] < look["worm_end"] <= 1:
        for key in ("worm_start", "worm_end"):
            look[key] = checker.bad(f"tray.glow.{key}", "should be 0 <= worm_start < worm_end <= 1",
                                    defaults["glow"][key])
    return data


def _env_number(name: str, default):
    """An env override of a data value (kept from before tray.json)."""
    try:
        return type(default)(os.environ[name])
    except (KeyError, ValueError):
        return default


def configure(path=None) -> dict:
    """(Re)load tray.json (or `path`, or $DENISSE_TRAY_CONFIG) and the
    module values built from it. Tests call it with their own file."""
    global CONFIG
    path = path or os.environ.get(CONFIG_ENV_VAR) or CONFIG_FILE
    CONFIG = json_config.load("tray", path, DEFAULTS_FILE, extra=_extra_checks)
    _apply(CONFIG)
    return CONFIG


def _text(*keys, **fields) -> str:
    """CONFIG['text'][k1][k2]... formatted with `fields`."""
    value = CONFIG["text"]
    for k in keys:
        value = value[k]
    return value.format(**fields) if fields else value


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + _text("chat", "ellipsis")


_reminder_stop_event = threading.Event()

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

def _apply(cfg: dict) -> None:
    """Module values from tray.json. Kept as module names (POPUP_BG, ...)
    because the drawing code and scripts/smoke_pet_chat.py read them."""
    global DASHBOARD_URL, ACCENT, BG, POPUP_BG, POPUP_BORDER, POPUP_TEXT, POPUP_MUTED
    global STATUS_OK, STATUS_WARN, STATUS_BAD, ACCENT_HEX, ACCENT_BRIGHT, HEADER_BG
    global BUBBLE_OUT_OUTLINE, BUBBLE_OUT_FILL, BUBBLE_OUT_FG, BUBBLE_IN_OUTLINE, BUBBLE_IN_FILL, BUBBLE_IN_FG
    global BUBBLE_CHAMFER, PANEL_BG, DISC_FILL, BUBBLE_PAD_X, BUBBLE_PAD_Y, BUBBLE_GAP, SEND_BTN_SIZE
    global _FONT_STACK, FONT_FAMILY, REMINDER_HOUR, NEWS_POLL_SECONDS, THINKING_MAX_MS
    global CHAT_WIDTH, CHAT_HEIGHT, HEARD_DISPLAY_MS, REVEAL_WAIT_S, SUPERSAMPLE
    col, chat, timing = cfg["colours"], cfg["chat"], cfg["timing"]
    DASHBOARD_URL = cfg["dashboard_url"].format(port=mission_control.PORT)

    # The accent is the orb's (ui_theme.json palette), so the tray icon,
    # popup and orb stay one colour.
    palette = ui_theme.load()["palette"]
    ACCENT = _hex_to_rgb(palette["accent"])
    ACCENT_HEX = palette["accent"]
    ACCENT_BRIGHT = palette["accent_bright"]  # active/outgoing outlines
    BG = _hex_to_rgb(col["icon_bg"])

    # Popup palette -- Tron-Legacy-inspired: pure black void, thin glowing
    # purple line-work on angular (chamfered, not rounded) panels, rather
    # than filled color blocks. HEADER_BG matches the body so the header
    # reads as one continuous panel, separated only by a thin glow line.
    POPUP_BG = col["popup_bg"]
    POPUP_BORDER = col["popup_border"]  # resting/inactive outlines
    POPUP_TEXT = col["text"]
    POPUP_MUTED = col["muted"]
    STATUS_OK, STATUS_WARN, STATUS_BAD = col["status_ok"], col["status_warn"], col["status_bad"]
    HEADER_BG = POPUP_BG
    BUBBLE_OUT_OUTLINE = ACCENT_BRIGHT  # your messages -- bright glowing outline
    BUBBLE_OUT_FILL = (*ACCENT, col["bubble_out_alpha"])  # faint tint, not a filled block
    BUBBLE_OUT_FG = col["bubble_out_fg"]
    BUBBLE_IN_OUTLINE = col["bubble_in_outline"]  # hers -- dim, no glow (hierarchy vs. yours)
    BUBBLE_IN_FILL = (*_hex_to_rgb(col["bubble_in_fill"]), col["bubble_in_alpha"])
    BUBBLE_IN_FG = POPUP_TEXT
    # Solid fills, for widgets that can't do PIL alpha compositing.
    PANEL_BG = col["panel_bg"]
    DISC_FILL = col["disc_fill"]  # send button's solid glowing disc

    BUBBLE_CHAMFER = chat["bubble_chamfer"]  # corner cut -- angular panel, not a pill
    BUBBLE_PAD_X, BUBBLE_PAD_Y = chat["bubble_pad_x"], chat["bubble_pad_y"]
    BUBBLE_GAP = chat["bubble_gap"]
    SEND_BTN_SIZE = chat["send_button"]
    SUPERSAMPLE = chat["supersample"]  # drawn this many times larger, then downsampled
    CHAT_WIDTH, CHAT_HEIGHT = chat["width"], chat["height"]

    # Preferred font stack, most modern-looking first -- resolved to whichever
    # is installed the first time a ChatPopup is built (needs a live Tk root).
    _FONT_STACK = tuple(cfg["fonts"]["stack"])
    FONT_FAMILY = cfg["fonts"]["fallback"]  # overwritten by _resolve_font_family()

    # Task reminders fire on the first check at or after this hour, once per
    # task per day.
    REMINDER_HOUR = _env_number("DENISSE_REMINDER_HOUR", timing["reminder_hour"])
    NEWS_POLL_SECONDS = timing["news_poll_s"]  # how often to check whether today's briefing is due
    THINKING_MAX_MS = timing["thinking_max_ms"]  # never leave "thinking" stuck if a "done" gets lost
    # How long the "heard: ..." live indicator stays up -- was 2.5s (too
    # quick to read), lengthened on request.
    HEARD_DISPLAY_MS = _env_number("DENISSE_HEARD_DISPLAY_MS", timing["heard_display_ms"])
    # Longest the streamed voice waits for the Tk thread to reveal the reply
    # bubble before speaking anyway (normally a few ms).
    REVEAL_WAIT_S = timing["reveal_wait_s"]


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


configure()


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
    scale = SUPERSAMPLE
    w, h = max(w, 1), max(h, 1)
    pts = _chamfered_points(w * scale, h * scale, chamfer * scale)

    img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    if glow:
        look = CONFIG["glow"]
        glow_layer = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
        ImageDraw.Draw(glow_layer).polygon(pts, outline=(*outline_rgb, look["outline_alpha"]),
                                           width=outline_w * scale * look["width_mult"])
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=outline_w * scale * look["outline_blur"]))
        img = Image.alpha_composite(img, glow_layer)

    draw = ImageDraw.Draw(img)
    draw.polygon(pts, fill=fill_rgba)
    draw.polygon(pts, outline=(*outline_rgb, 255), width=outline_w * scale)

    img = img.resize((w, h), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


def _glow_line_image(w: int, rgb: tuple, thickness: int = 1, glow_pad: int = None) -> ImageTk.PhotoImage:
    """A thin horizontal glowing line -- stands in for a filled bar as a
    boundary marker (e.g. under the chat popup's header)."""
    look = CONFIG["glow"]
    glow_pad = look["line_pad"] if glow_pad is None else glow_pad
    scale = SUPERSAMPLE
    h = thickness + glow_pad * 2
    img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    cy = h * scale // 2
    glow_layer = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    ImageDraw.Draw(glow_layer).line([(0, cy), (w * scale, cy)], fill=(*rgb, look["outline_alpha"]),
                                    width=thickness * scale * look["width_mult"])
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=glow_pad * scale * look["line_blur"]))
    img = Image.alpha_composite(img, glow_layer)
    ImageDraw.Draw(img).line([(0, cy), (w * scale, cy)], fill=(*rgb, 255), width=thickness * scale)
    img = img.resize((w, h), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


def _circle_image(d: int, rgb: tuple, glow: bool = False) -> tuple:
    """A filled circle, optionally with a soft blurred glow halo behind it
    (a restrained neon touch -- used for the send button's active state)."""
    scale = SUPERSAMPLE
    look = CONFIG["glow"]
    # modest room for the blur to bleed into -- kept small deliberately, restrained not neon-billboard
    pad = max(look["disc_pad_min"], int(d * look["disc_pad_frac"])) if glow else 0
    size = d + pad * 2
    img = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if glow:
        glow_layer = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
        ImageDraw.Draw(glow_layer).ellipse(
            (pad * scale, pad * scale, (pad + d) * scale, (pad + d) * scale), fill=(*rgb, look["disc_alpha"])
        )
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=d * scale * look["disc_blur"]))
        img = Image.alpha_composite(img, glow_layer)
        draw = ImageDraw.Draw(img)
    draw.ellipse((pad * scale, pad * scale, (pad + d) * scale, (pad + d) * scale), fill=(*rgb, 255))
    img = img.resize((size, size), Image.LANCZOS)
    return ImageTk.PhotoImage(img), pad


def _side_glow_image(w: int, h: int, rgb: tuple) -> ImageTk.PhotoImage:
    """A soft vertical glow blob, bright in the middle and fading at both
    ends -- the traveling 'light worm' segment run up/down the chat popup's
    left/right edges."""
    scale = SUPERSAMPLE
    img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    cx = w * scale // 2
    glow_layer = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    look = CONFIG["glow"]
    ImageDraw.Draw(glow_layer).line(
        [(cx, h * scale * look["worm_start"]), (cx, h * scale * look["worm_end"])],
        fill=(*rgb, look["worm_alpha"]), width=w * scale
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=h * scale * look["worm_blur"]))
    img = Image.alpha_composite(img, glow_layer)
    img = img.resize((w, h), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


def _make_icon_image(size: int = None) -> Image.Image:
    """Draw a simple letter badge so Denisse doesn't need a shipped icon asset."""
    icon = CONFIG["icon"]
    size = size or icon["size"]
    ring = icon["ring_width"]
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((ring - 1, ring - 1, size - ring, size - ring), fill=BG, outline=ACCENT, width=ring)

    text = icon["letter"]
    font = None
    for font_name in CONFIG["fonts"]["icon_files"]:
        try:
            font = ImageFont.truetype(font_name, int(size * icon["letter_frac"]))
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
        urllib.request.urlopen(DASHBOARD_URL, timeout=CONFIG["timing"]["dashboard_probe_s"])
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
_orb_controls = None

_agent_engine = None
_agent_threads = []


def _get_agent_engine():
    global _agent_engine
    if _agent_engine is None:
        _agent_engine = standing_instructions.Engine()
    return _agent_engine


def _is_organizing() -> bool:
    settings = _get_agent_engine().status()
    return settings["enabled"] and any(j["id"] == "downloads" and j["enabled"] for j in settings["jobs"])


def _start_organizing():
    _get_agent_engine().set_enabled(True, "downloads")


def _stop_organizing():
    _get_agent_engine().set_enabled(False, "downloads")


def _toggle_organize(icon, item):
    if _is_organizing():
        _stop_organizing()
    else:
        _start_organizing()


def _toggle_independent_work(icon=None, item=None):
    engine = _get_agent_engine()
    engine.set_enabled(not engine.status()["enabled"])


def _show_agent_activity(icon=None, item=None):
    import json
    engine = _get_agent_engine()
    try:
        state = json.loads(engine.state.read_text(encoding="utf-8")) if engine.state.exists() else {}
        text = "\n\n".join(value.get("brief", _text("activity", "in_progress")) for value in state.values())
        text = text or _text("activity", "none_yet")
    except (OSError, ValueError, AttributeError):
        text = _text("activity", "unreadable")
    error = engine.status()["error"]
    _show_reply((error + "\n" if error else "") + text[:CONFIG["limits"]["activity_chars"]])


def _start_agent_workers():
    engine = _get_agent_engine()
    if not engine.config.exists():
        # Preserve the existing Downloads-only behaviour for older installs.
        # New model-driven reviews are enabled explicitly by setup/the user.
        legacy = standing_instructions.defaults()
        legacy["enabled"] = True
        for job in legacy["jobs"]:
            job["enabled"] = job["id"] == "downloads"
        standing_instructions.atomic_json(engine.config, legacy)
    if any(thread.is_alive() for thread in _agent_threads):
        return
    engine.stop.clear()
    def notify(result):
        if _tray_icon is not None:
            _tray_icon.notify(result["brief"][:CONFIG["limits"]["notify_chars"]],
                              _text("notify", "agent_job", job=result["job_id"].replace("_", " ")))
    for jobs in (("downloads",), ("task_review", "project_review")):
        ready = (lambda: True) if jobs == ("downloads",) else (
            lambda: not _is_manually_offline() and not _thinking_sources)
        worker = threading.Thread(target=engine.serve, args=(jobs, notify, ready), daemon=True)
        _agent_threads.append(worker)
        worker.start()


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
                    _tray_icon.notify(warning, _text("notify", "title"))
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

    limits = CONFIG["limits"]
    try:
        _tray_icon.notify(_clip(reply, limits["wake_reply_chars"]),
                          _text("notify", "wake_word", command=_clip(command, limits["wake_command_chars"])))
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
    elif mode == "online":
        _go_online()
    elif mode == "study":
        _start_study_mode()
    elif mode == "stop_study":
        _stop_study_mode()


def _wake_word_loop():
    wake_word.run(
        stop_event=_wake_word_stop_event,
        on_exchange=_on_wake_word_exchange,
        on_heard=_on_wake_word_heard,
        is_offline=_is_manually_offline,
        on_mode_command=_on_wake_word_mode_command,
        on_thinking=lambda on: _set_thinking("wake", on),
        on_reply=_show_reply,
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
# demand, triggered only by typing an exact offline phrase (tray.json
# commands.offline_phrases, shipped "offline mode"; back with an online
# phrase) or saying wake_word.py's own, never by clicking anything. Separate
# from whether Ollama itself is actually reachable (that's still what
# _status_display's connected/model_ready checks reflect -- this just
# forces the display to "Offline" and blocks new activity on top of that).
# The wake-word listener keeps running while offline -- it has to, in
# order to still catch "online mode" being said -- it just discards
# everything else it hears in the meantime (see wake_word.run's is_offline
# param); same idea for typed chat (see ChatPopup._send).
_manual_offline = threading.Event()

def _match_mode_command(text: str):
    """-> 'offline', 'online', or None, matching wake_word.py's existing
    "exit"/"quit"/"stop listening" convention: the phrase, and nothing
    else, modulo case/surrounding whitespace/trailing punctuation. The
    phrases are tray.json commands.offline_phrases / online_phrases -- a
    fixed control word, not a guess at meaning: it has to work when the
    model can't."""
    said = text.strip().lower().rstrip(".!")
    for mode in ("offline", "online"):
        if said in (p.strip().lower() for p in CONFIG["commands"][f"{mode}_phrases"]):
            return mode
    return None


def _online_phrase() -> str:
    """The phrase the offline notices tell him to type."""
    return next(p for p in CONFIG["commands"]["online_phrases"] if p.strip()).strip()


# "show reminder", "show my reminders", "open the task list", or just
# "reminders" -- typed in the chat, this opens the reminder card instead of
# asking the model (asked 2026-10-07, in place of right-clicking the orb).
# The regex is tray.json commands.reminders; whole-message only, so "remind
# me to call the dentist" still goes to the model and becomes a task.
def _is_reminder_command(text: str) -> bool:
    return bool(re.match(CONFIG["commands"]["reminders"], " ".join(text.lower().rstrip(".!?").split())))


def _route(name: str) -> str:
    """tray.json commands.route.<name>: 'tray' or 'model'."""
    return CONFIG["commands"]["route"][name]


def _tray_handles(name: str, brain_ready: bool) -> bool:
    """Does the tray act on a matched `name` command itself? Always on the
    'tray' route; on the 'model' route only while there is no model to ask,
    so a matched command never just does nothing."""
    return _route(name) == "tray" or not brain_ready


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


_news_thread = None
_news_stop_event = threading.Event()


def _decline_risky_unattended(name, args):
    """No human is present to answer a confirmation dialog for this
    background-triggered brain call (the daily news briefing) -- auto-
    decline anything RISKY_TOOLS would otherwise ask about, rather than
    either hanging forever waiting for input or running it unsupervised."""
    return name not in RISKY_TOOLS


def _reminder_loop():
    """Check every tray.json timing.reminder_poll_s (15 minutes shipped),
    so the nudge arrives daily and not only at login. The machine stays on for days at a time (5+ days uptime is
    normal here), so a login-only reminder can go a whole week without
    firing. needs_reminder() already dedupes per task per day, so a
    frequent poll costs nothing and simply catches the first check after
    the morning hour.
    """
    while not _reminder_stop_event.is_set():
        now = datetime.now()
        if now.hour >= REMINDER_HOUR:
            _remind_due_tasks()
        _reminder_stop_event.wait(CONFIG["timing"]["reminder_poll_s"])


def _session_locked() -> bool:
    """True while the lock screen is up: the input desktop is then
    Winlogon's, which a normal process can't open. Any failure to check
    reads as unlocked, so the worst case is a missed bubble, not a stuck
    watcher."""
    try:
        user32 = ctypes.windll.user32
        desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not desk:
            return True
        user32.CloseDesktop(desk)
        return False
    except Exception:
        return False


def _unlock_watch_loop():
    """Show the reminder bubble every time he comes back to the machine,
    not just at login -- it stays on for days (see _reminder_loop), so
    "every time I open my computer" (asked 2026-10-07) mostly means
    unlocking it or waking it from sleep, and the tray doesn't restart for
    either. Polls rather than hooking WTS session messages because Tk owns
    the window procedure. Two signals: the lock screen going away, and a
    jump in wall-clock time (resumed from sleep without a lock screen).
    """
    timing = CONFIG["timing"]
    poll = timing["unlock_poll_s"]
    was_locked = _session_locked()
    last_tick = time.time()
    last_shown = 0.0
    while not _reminder_stop_event.wait(poll):
        now = time.time()
        locked = _session_locked()
        slept = now - last_tick > poll + timing["sleep_jump_s"]
        last_tick = now
        if locked:
            was_locked = True
            continue
        if (was_locked or slept) and now - last_shown > timing["unlock_min_gap_s"]:
            last_shown = now
            # A beat after unlocking, so it arrives once the desktop is back.
            _reminder_stop_event.wait(timing["unlock_settle_s"])
            _remind_due_tasks(startup=True)
        was_locked = False


_reminder_card = None


def _show_reminder_card():
    """Pop the reminder card out of the orb (see reminder_card.py), or
    refresh it if it's already up. Main thread only -- background threads
    go through root.after(0, ...)."""
    global _reminder_card
    if _floating_orb is None:
        return
    if _reminder_card is not None:
        _reminder_card.refresh()
        return

    def _closed():
        global _reminder_card
        _reminder_card = None

    _close_thinking_bubble()  # one bubble beside the orb at a time
    _close_study_bubble()
    _close_reply_bubble()
    _reminder_card = ReminderCard(root, _floating_orb, tasks, font_family=FONT_FAMILY, on_close=_closed)


def _open_reminders(icon=None, item=None):
    def show():
        if _chat_popup is not None:
            _chat_popup.close()
        _show_reminder_card()
    root.after(0, show)


# Study mode (study_mode.py): 25 min study / 5 min rest, each switch texted
# from the orb like the reminder card. The clock lives on the Tk thread.
_study_mode = None
_study_bubble = None


def _show_study_bubble(phase, ends_at, round_no, status=False):
    """study_mode's hook for a phase change (or, with status=True, a click
    on its countdown badge). Main thread only. The alert is time-sensitive,
    so it takes the orb's spot from whatever is there."""
    global _study_bubble
    if _floating_orb is None:
        if _tray_icon is not None:
            title = _text("notify", "study_phase_study" if phase == "study" else "study_phase_rest")
            _tray_icon.notify(_text("notify", "study_until", ends_at=f"{ends_at:%H:%M}"),
                              _text("notify", "study", phase=title))
        return
    _close_study_bubble()
    _close_thinking_bubble()
    _close_reply_bubble()
    if _reminder_card is not None:
        _reminder_card.close()

    def _closed():
        global _study_bubble
        if _study_bubble is bubble:
            _study_bubble = None

    bubble = StudyBubble(
        root, _floating_orb, phase, ends_at, round_no, on_stop=_stop_study_mode_main,
        font_family=FONT_FAMILY, on_close=_closed, status=status,
    )
    _study_bubble = bubble


def _close_study_bubble():
    if _study_bubble is not None:
        _study_bubble.close()  # its on_close clears _study_bubble


def _stop_study_mode_main():
    if _study_mode is not None and _study_mode.active:
        _study_mode.stop()
        _close_study_bubble()
        _show_reply_main(_text("study_off"))
    if _tray_icon is not None:
        _tray_icon.update_menu()


def _start_study_mode():
    def _do():
        _study_mode.start()
        if _tray_icon is not None:
            _tray_icon.update_menu()
    root.after(0, _do)


def _stop_study_mode():
    root.after(0, _stop_study_mode_main)


def _on_study_change(mode):
    """After the clock starts, stops, pauses or resumes -- from the menu,
    the bubble, a typed phrase or one of the brain's study tools. Main
    thread only (StudyMode calls it from its own methods)."""
    if not mode.active:
        _close_study_bubble()
    if _tray_icon is not None:
        _tray_icon.update_menu()


def _on_tk_thread(fn, timeout_s=None):
    """Run `fn` on the Tk thread and return its result. Called from the
    brain's worker thread by study_mode's tool functions; a direct call
    when already on the main thread, so the CLI and tests don't deadlock."""
    if threading.current_thread() is threading.main_thread():
        return fn()
    done = threading.Event()
    box = {}

    def run():
        try:
            box["result"] = fn()
        except Exception as exc:  # reported to the model as the tool's error
            box["error"] = exc
        finally:
            done.set()

    root.after(0, run)
    if not done.wait(timeout_s or CONFIG["timing"]["tk_call_timeout_s"]):
        raise RuntimeError(_text("tk_timeout"))
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _toggle_study_mode(icon=None, item=None):
    if _study_mode is not None and _study_mode.active:
        _stop_study_mode()
    else:
        _start_study_mode()


def _study_menu_text(item):
    status = _study_mode.status() if _study_mode is not None else ""
    if status:
        return _text("menu", "study_running", status=status)
    return _text("menu", "study_idle", study_min=study_mode.STUDY_MIN, rest_min=study_mode.REST_MIN)


# Who is currently composing a reply ("chat", "wake"). A set rather than a
# flag so a typed reply and a "hey Denisse" one overlapping don't hide the
# bubble when only the first finishes; idempotent so a double "done" from
# an error path can't go negative.
_thinking_sources = set()
_thinking_bubble = None
_thinking_timeout_job = None


def _runtime_state():
    """In-memory state for runtime_status.py, read on its own thread, so
    a restart can be judged from outside: timers and study mode exist
    nowhere else."""
    import timers
    study = _study_mode
    try:
        runs = _get_agent_engine().status().get("last_runs") or {}
    except Exception:
        runs = {}
    when = lambda t: t.isoformat(timespec="seconds") if t is not None else None
    return {"timers": [{"label": t.get("label") or "", "ends_at": when(t.get("ends_at"))} for t in timers.active()],
            "study": {"active": bool(study is not None and study.active),
                      "phase": getattr(study, "phase", None), "ends_at": when(getattr(study, "ends_at", None))},
            "chat_busy": bool(_thinking_sources),
            "jobs": {k: v.get("status") for k, v in runs.items() if isinstance(v, dict)}}


def _set_thinking(source: str, on: bool):
    """Show the "typing" bubble out of the orb while Denisse composes a
    reply (see reminder_card.ThinkingBubble). Safe from any thread."""
    root.after(0, lambda: _set_thinking_main(source, on))


def _set_thinking_main(source: str, on: bool):
    global _thinking_bubble, _thinking_timeout_job
    if on:
        _thinking_sources.add(source)
    else:
        _thinking_sources.discard(source)
    if _floating_orb is not None:
        _floating_orb.set_thinking(bool(_thinking_sources))
    if not _thinking_sources:
        _close_thinking_bubble()
        if _chat_popup is not None and not _chat_popup.busy:
            _chat_popup.typing_label.config(text="")
        return
    if _chat_popup is not None:
        _chat_popup.typing_label.config(text=_text("chat", "thinking"))
        return  # the attached chat is already the pet's conversation bubble
    # The reminder card already sits beside the orb; don't stack a second.
    if _thinking_bubble is None and _reminder_card is None and _study_bubble is None and _floating_orb is not None:
        _close_reply_bubble()  # the last reply makes way for the next "• • •"

        def _closed():
            global _thinking_bubble
            if _thinking_bubble is bubble:
                _thinking_bubble = None

        try:
            bubble = ThinkingBubble(root, _floating_orb, font_family=FONT_FAMILY, on_close=_closed,
                                    on_reply=_open_chat, on_stop=_stop_speaking)
            _thinking_bubble = bubble
        except Exception:
            _thinking_bubble = None  # cosmetic -- never let it break a reply
    if _thinking_timeout_job is not None:
        root.after_cancel(_thinking_timeout_job)
    _thinking_timeout_job = root.after(THINKING_MAX_MS, _thinking_timed_out)


def _thinking_timed_out():
    global _thinking_timeout_job
    _thinking_timeout_job = None
    _thinking_sources.clear()
    if _floating_orb is not None:
        _floating_orb.set_thinking(False)
    _close_thinking_bubble()


def _close_thinking_bubble():
    if _thinking_bubble is not None:
        _thinking_bubble.close()  # its on_close clears _thinking_bubble


# Her reply, shown in the bubble out of the orb (asked 2026-10-14: "whenever
# she speaks I want her message to pop up like the ... animation"). If the
# "• • •" bubble is up it becomes the reply; otherwise a fresh bubble types
# for a beat and then shows it. See ThinkingBubble.deliver.
_reply_bubble = None


def _show_reply(text: str):
    """Safe from any thread."""
    root.after(0, lambda: _show_reply_main(text))


def _show_reply_main(text: str, _retry: bool = False, source=None):
    global _thinking_bubble, _reply_bubble
    if _floating_orb is None or not (text or "").strip():
        return
    if not _retry:
        _floating_orb.react("happy", CONFIG["timing"]["happy_react_s"])
    if _chat_popup is not None:
        if source != "chat":
            _chat_popup.receive_reply(text)
        _close_thinking_bubble()
        _close_reply_bubble()
        return
    if _reminder_card is not None and _reminder_card.editing is not None:
        return  # he's mid-edit in the reminder card -- don't take it away
    try:
        def _closed():
            global _reply_bubble
            if _reply_bubble is bubble:
                _reply_bubble = None

        _close_reply_bubble()
        if _thinking_bubble is not None:
            bubble = _thinking_bubble
            _thinking_bubble = None  # it's the reply now; "thinking off" mustn't close it
            bubble.on_close = _closed
            if not bubble.deliver(text):
                # Its window had already gone. Build a fresh bubble rather
                # than let the reply land nowhere -- see _deliver_reply.
                _show_reply_main(text, _retry=True)
                return
            _reply_bubble = bubble
        else:
            _close_study_bubble()
            if _reminder_card is not None:
                _reminder_card.close()
            bubble = ThinkingBubble(root, _floating_orb, font_family=FONT_FAMILY, on_close=_closed,
                                    on_reply=_open_chat, on_stop=_stop_speaking)
            _reply_bubble = bubble
            # A retry has already waited through one typing pause; show it
            # straight away so the gap can't be interrupted a second time.
            root.after(0 if _retry else TYPING_MS,
                       lambda b=bubble: _deliver_reply(b, text, _retry))
    except Exception:
        pass  # cosmetic -- the reply itself is already in the chat / spoken


def _deliver_reply(bubble, text: str, retried: bool):
    """Land the reply in `bubble`, or rebuild once if that bubble has gone.

    A fresh bubble types for TYPING_MS before the message lands, and that
    pause is a window in which anything calling _close_reply_bubble()
    destroys it: the reminder card on its 15-minute loop or on an unlock,
    a pomodoro phase turning over, or a second question starting its own
    "thinking" bubble (ask by voice, then start typing before her answer
    renders). deliver() swallowed the resulting TclError and the reply was
    never shown -- with the chat popup closed and TTS off, the whole
    visible answer to a "hey Denisse" simply vanished.

    One retry, not a loop: if something is closing bubbles that
    persistently, a second rebuild would just feed it.
    """
    if bubble.deliver(text):
        return
    if retried:
        return
    _show_reply_main(text, _retry=True)


def _close_reply_bubble():
    if _reply_bubble is not None:
        _reply_bubble.close()  # its on_close clears _reply_bubble


def _remind_due_tasks(startup: bool = False):
    """Show what's on his plate as a chat bubble out of the orb.

    At startup (login) and on every unlock/wake (_unlock_watch_loop) the
    card shows the whole open list -- the "first thing when the computer
    opens" view, requested 2026-10-05 -- even when it's empty, so he sees
    it every time (asked again 2026-10-07; an empty list used to get only
    the orb's one-line speech bubble, which he read as it not showing).
    After that, the 15-minute loop only pops it again for
    something overdue or due within 48h that hasn't been flagged today, so
    it nags once a day rather than on every check.

    Deliberately no model call -- it costs no model time, can't
    hallucinate, and works even if Ollama hasn't finished starting (which
    it often hasn't, right after a reboot). Falls back to a tray
    notification if the orb isn't up for some reason.
    """
    try:
        tasks.pull_vault_ticks()  # respect anything ticked in Obsidian
        overdue, soon = tasks.needs_reminder(hours=CONFIG["timing"]["reminder_window_hours"])
        if not startup and not (overdue or soon):
            return
        if _floating_orb is not None:
            root.after(0, _show_reminder_card)
        elif _tray_icon is not None and (overdue or soon):
            limits = CONFIG["limits"]
            shown = limits["plate_lines"]
            lines = [_text("notify", "overdue", task=tasks.describe(t)) for t in overdue]
            lines += [tasks.describe(t) for t in soon]
            body = "\n".join(lines[:shown])
            if len(lines) > shown:
                body += "\n" + _text("notify", "more", count=len(lines) - shown)
            _tray_icon.notify(body[:limits["notify_chars"]], _text("notify", "plate"))
        tasks.mark_reminded([t["id"] for t in overdue + soon])
    except Exception:
        pass  # a reminder failing must never take the tray down with it


def _news_briefing_loop():
    while not _news_stop_event.is_set():
        # Checked every poll, not just at startup: Denisse can now switch
        # the briefing off when asked (brain._NEWS_OFF), and that has to
        # take effect without a tray restart.
        if news.briefing_enabled() and news.is_daily_briefing_due():
            try:
                brain = conversation.get_brain()
                messages = conversation.replay()
                turn_start = len(messages)
                messages.append({"role": "user", "content": _text("news_prompt")})
                reply, messages = brain.respond(messages, confirm=_decline_risky_unattended)
                conversation.record(messages[turn_start:])
                news.mark_daily_briefing_done()
                _show_reply(reply)

                # The briefing used to also write a dated note into the
                # vault's News/ folder. Removed 2026-09-06 at Chuen's
                # request: he never went back and read them, and a new
                # file every day was quietly accumulating clutter (each
                # one an unlinked node in Obsidian's graph). The briefing
                # itself still runs and still notifies.

                if _tray_icon is not None:
                    try:
                        _tray_icon.notify(reply[:CONFIG["limits"]["notify_chars"]], _text("notify", "news"))
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
    # Persisted, so "off" survives the restart at every login -- main()
    # used to start the briefing unconditionally, so switching it off here
    # only lasted until the next boot.
    if _is_news_briefing_active():
        _stop_news_briefing()
        news.set_briefing_enabled(False)
    else:
        news.set_briefing_enabled(True)
        _start_news_briefing()


def _quit(icon, item):
    if _agent_engine is not None:
        _agent_engine.stop.set()
    _stop_optimizing()
    _stop_wake_word_listening()
    _stop_news_briefing()
    if _global_hotkey is not None:
        _global_hotkey.stop()
    if _explain_hotkey is not None:
        _explain_hotkey.stop()
    icon.stop()
    root.after(0, root.quit)


def _status_display(status: dict) -> tuple:
    """-> (dot_color, message): just online/offline, no model name."""
    if _is_manually_offline():
        return POPUP_MUTED, _text("status", "offline")
    if not status.get("connected"):
        return STATUS_BAD, _text("status", "offline")
    if not status.get("model_ready"):
        return STATUS_WARN, _text("status", "online")
    return STATUS_OK, _text("status", "online")


def _stop_speaking():
    """Immediate speech control, including replies still synthesizing."""
    wake_word.interrupt()
    if _chat_popup is not None:
        _chat_popup.typing_label.config(text=_text("chat", "speech_stopped"))


class ChatPopup:
    """A conversation flyout anchored to the desktop pet."""

    def __init__(self):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)  # no title bar -- a flyout, not a window
        self.win.attributes("-topmost", True)
        self.win.configure(bg=POPUP_BG, highlightbackground=POPUP_BORDER, highlightthickness=1)

        area = _work_area(self.win.winfo_screenwidth(), self.win.winfo_screenheight())
        chat, fonts = CONFIG["chat"], CONFIG["fonts"]
        pad = chat["padding"]
        w = min(_px(CHAT_WIDTH), area[2] - area[0] - _px(chat["screen_margin"]))
        h = min(_px(CHAT_HEIGHT), area[3] - area[1] - _px(chat["screen_margin"]))
        self._window_size = w, h
        self._anchor_job = None
        self._anchor_geometry = None
        self._follow_anchor()
        self.win.bind("<Escape>", lambda e: self.close())

        # -- header: name + online/offline status. No filled bar (Tron panels
        # are dark glass, not color blocks) -- just a glowing hairline
        # separator below (added after this block) marks the boundary. --
        header = tk.Frame(self.win, bg=HEADER_BG)
        header.pack(side="top", fill="x")
        name_col = tk.Frame(header, bg=HEADER_BG)
        name_col.pack(side="left", padx=_px(pad["header_x"]), pady=_px(pad["header_y"]), fill="x", expand=True)
        tk.Label(
            name_col, text=_text("popup_title"), bg=HEADER_BG, fg=POPUP_TEXT, font=(FONT_FAMILY, fonts["title"], "bold")
        ).pack(anchor="w")
        # Status dot + text -- toggled by saying or typing an offline/online
        # phrase (see _match_mode_command), not by clicking anything.
        status_sub = tk.Frame(name_col, bg=HEADER_BG)
        status_sub.pack(anchor="w")
        self._status_dot = tk.Canvas(
            status_sub, width=_px(chat["status_dot"]), height=_px(chat["status_dot"]), bg=HEADER_BG,
            highlightthickness=0,
        )
        self._status_dot_id = self._status_dot.create_oval(1, 1, _px(chat["status_dot"] - 1),
                                                           _px(chat["status_dot"] - 1), fill="", outline="")
        self._status_dot.pack(side="left")
        self._status_label = tk.Label(
            status_sub, text="", bg=HEADER_BG, fg=POPUP_MUTED, font=(FONT_FAMILY, fonts["status"])
        )
        self._status_label.pack(side="left", padx=(_px(pad["status_gap"]), 0))
        tk.Button(
            header, text=_text("chat", "close"), command=self.close, bg=HEADER_BG, fg=POPUP_MUTED,
            activebackground=HEADER_BG, activeforeground=POPUP_TEXT, relief="flat", bd=0,
            font=(FONT_FAMILY, fonts["close"]), cursor="hand2",
        ).pack(side="right", padx=_px(pad["close_x"]))
        tk.Button(header, text=_text("chat", "stop_voice"), command=_stop_speaking,
                  bg=HEADER_BG, fg=POPUP_MUTED, activebackground=HEADER_BG,
                  activeforeground=POPUP_TEXT, relief="flat", bd=0,
                  font=(FONT_FAMILY, fonts["stop_voice"]), cursor="hand2").pack(side="right")

        # Thin glowing line standing in for the header's old filled bar --
        # the one place a Tron panel actually earns a solid glow, since it
        # marks a real boundary (header vs. chat body) rather than filling
        # a whole surface.
        self._header_sep_img = _glow_line_image(_px(CHAT_WIDTH), _hex_to_rgb(ACCENT_HEX))
        tk.Label(self.win, image=self._header_sep_img, bg=POPUP_BG, bd=0).pack(side="top")

        # -- bottom-anchored input row, packed BEFORE the chat canvas so it
        # always keeps its space regardless of how tall the message list gets --
        input_row = tk.Frame(self.win, bg=POPUP_BG)
        input_row.pack(side="bottom", fill="x", padx=_px(pad["input_x"]),
                       pady=(_px(pad["input_top"]), _px(pad["input_bottom"])))
        self.entry = tk.Entry(
            input_row, bg=PANEL_BG, fg=POPUP_TEXT, insertbackground=POPUP_TEXT,
            relief="flat", font=(FONT_FAMILY, fonts["entry"]), state="disabled",
            # Dim glow-line purple at rest, brightens to the full accent on
            # focus -- same outline language as the chat panels.
            highlightthickness=1, highlightbackground=POPUP_BORDER, highlightcolor=ACCENT_HEX,
            # Tk's default disabled-state colors are a light system grey/white,
            # unrelated to bg/fg above -- set these too or the box flashes
            # white every time it's disabled (sending, before Denisse connects).
            disabledbackground=PANEL_BG, disabledforeground=POPUP_MUTED,
        )
        self.entry.pack(
            side="left", fill="x", expand=True, ipady=_px(pad["entry_ipad_y"]), ipadx=_px(pad["entry_ipad_x"]),
            padx=(0, _px(pad["entry_gap"]))
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
            center, center, text=_text("chat", "send"), fill=POPUP_MUTED, font=(FONT_FAMILY, fonts["send"])
        )
        self.send_btn.tag_bind(self._send_circle_id, "<Button-1>", self._send_clicked)
        self.send_btn.tag_bind(self._send_arrow_id, "<Button-1>", self._send_clicked)

        self.typing_label = tk.Label(
            self.win, text="", bg=POPUP_BG, fg=POPUP_MUTED, font=(FONT_FAMILY, fonts["typing"], "italic"),
            justify="left", wraplength=_px(CHAT_WIDTH - chat["typing_inset"]),
        )
        self.typing_label.pack(side="bottom", fill="x", padx=_px(pad["typing_x"]))

        # -- chat area: a Canvas of hand-drawn rounded bubbles, no scrollbar --
        self.canvas = tk.Canvas(self.win, bg=POPUP_BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=_px(pad["canvas_x"]),
                         pady=(_px(pad["canvas_top"]), _px(pad["canvas_bottom"])))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self._font = tkfont.Font(family=FONT_FAMILY, size=fonts["body"])
        self._time_font = tkfont.Font(family=FONT_FAMILY, size=fonts["time"])
        self._y = _px(chat["bubble_margin"])
        self._bubble_images = []  # keep PhotoImage refs alive (Tk won't otherwise)

        # -- side glow: a soft purple "light worm" traveling up the left
        # edge and down the right edge, on a loop -- placed (not packed) so
        # it overlays the existing layout as a thin strip rather than
        # taking up its own space. --
        strip_w = _px(chat["glow_strip_w"])
        self._glow_strip_h = _px(chat["glow_strip_h"])
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
        self._speech_epoch = speech_control.capture()
        self._trace = None
        self.refresh_status()
        self._render_history()

        self._connecting_y = self._y
        self._add_system(_text("chat", "connecting"), tag="connecting")
        threading.Thread(target=self._init_brain, daemon=True).start()

    # -- startup --

    def _follow_anchor(self):
        try:
            if not self.win.winfo_exists():
                return
            area = _work_area(self.win.winfo_screenwidth(), self.win.winfo_screenheight())
            orb = None
            if _floating_orb is not None:
                ow = _floating_orb.win
                orb = (ow.winfo_x(), ow.winfo_y(), _floating_orb.total_px)
            w, h = self._window_size
            chat = CONFIG["chat"]
            x, y, _, _ = _place(w, h, 0, area, _px(chat["anchor_margin"]), orb=orb, gap=-_px(chat["anchor_gap"]))
            geometry = f"{w}x{h}{x:+d}{y:+d}"
            if geometry != self._anchor_geometry:
                self.win.geometry(geometry)
                self._anchor_geometry = geometry
            self._anchor_job = self.win.after(CONFIG["timing"]["anchor_poll_ms"], self._follow_anchor)
        except tk.TclError:
            pass

    def receive_reply(self, text):
        """Voice replies join the same conversation when its flyout is open."""
        if not self.busy and self.brain is not None:
            history = conversation.replay()
            latest = next((m.get("content", "") for m in reversed(history)
                           if m.get("role") == "assistant"), "")
            if latest == text:
                self.messages = history
                self.canvas.delete("all")
                self._bubble_images.clear()
                self._y = _px(CONFIG["chat"]["bubble_margin"])
                self._render_history()
                return
        # Timer/study alerts aren't conversation records. They still have
        # to be visible when chat is open, without writing a fake exchange.
        self._add_bubble("denisse", text)

    def _init_brain(self):
        try:
            # Shared with the wake-word listener and the news briefing --
            # one Brain per process, not one per face.
            brain, err = conversation.get_brain(), None
        except RuntimeError as e:
            brain, err = None, str(e)
        root.after(0, lambda: self._brain_ready(brain, err))

    def _brain_ready(self, brain, err):
        try:
            if not self.win.winfo_exists():
                return
        except tk.TclError:
            return
        self.brain = brain
        self.canvas.delete("connecting")
        bounds = self.canvas.bbox("all")
        self._y = bounds[3] + _px(BUBBLE_GAP) if bounds else _px(CONFIG["chat"]["bubble_margin"])
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
            self._send_arrow_id, fill=BUBBLE_OUT_FG if enabled else POPUP_MUTED,
            text=_text("chat", "send" if enabled else "stop")
        )
        self.send_btn.config(cursor="hand2")

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
        return w if w > 1 else _px(CHAT_WIDTH) - _px(CONFIG["chat"]["canvas_inset"])

    def _autoscroll(self):
        self.canvas.config(scrollregion=(0, 0, self._canvas_w(), self._y))
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def _add_bubble(self, who: str, text: str):
        is_out = who == "you"
        chat = CONFIG["chat"]
        pad_x, pad_y, gap, margin = _px(BUBBLE_PAD_X), _px(BUBBLE_PAD_Y), _px(BUBBLE_GAP), _px(chat["bubble_margin"])
        lines = []
        for paragraph in text.splitlines() or [""]:
            lines.extend(self._wrap_text(paragraph, self._canvas_w() - _px(chat["bubble_side_room"])))
        line_h = self._font.metrics("linespace")
        ts = datetime.now().strftime(_text("chat", "time_format"))
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
            outline_w=max(1, _px(chat["bubble_outline"])),
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
            x2 - pad_x, y1 + bh - _px(chat["time_inset"]), text=ts, anchor="se", fill=fg, font=self._time_font,
        )

        self._y = y1 + bh + gap
        self._autoscroll()

    def _add_system(self, text: str, tag: str = None):
        canvas_w = self._canvas_w()
        lines = self._wrap_text(text, canvas_w - _px(CONFIG["chat"]["system_side_room"]))
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
        self.canvas.yview_scroll(int(-1 * (event.delta / CONFIG["chat"]["wheel_delta"])), "units")

    def _render_history(self):
        for m in self.messages:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role == "user" and content:
                self._add_bubble("you", content)
            elif role == "assistant" and content:
                self._add_bubble("denisse", content)

    # -- sending --

    def _send_clicked(self, event=None):
        if self.busy:
            _stop_speaking()  # stop square works even with a follow-up draft
            return "break"
        return self._send(event)

    def _send(self, event=None):
        text = self.entry.get().strip()
        if speech_control.is_stop_request(text) or (self.busy and not text):
            self.entry.delete(0, "end")
            _stop_speaking()
            return "break"
        if not text:
            return "break"

        # Mode commands work regardless of busy/brain state -- offline mode
        # in particular has to be reliable even if Ollama is unreachable or
        # a reply is still composing, not just when everything's healthy.
        mode = _match_mode_command(text)
        if mode == "offline":
            self.entry.delete(0, "end")
            _go_offline()
            self._add_system(_text("chat", "went_offline", online=_online_phrase()))
            return "break"
        if mode == "online":
            self.entry.delete(0, "end")
            _go_online()
            self._add_system(_text("chat", "back_online"))
            return "break"

        # On the 'tray' route (tray.json commands.route): straight to the
        # clock / off disk, no model call -- works even while a reply is
        # composing or Ollama is down. On the 'model' route the message goes
        # to the model below, which acts through its own tools (the study_*
        # set, list_tasks) and keeps the turn in her history; the tray only
        # steps in while there's no model to ask. The chat steps aside so
        # the card, which sits beside the orb, isn't hidden behind it --
        # unless a reply is still on its way, which closing would throw away.
        brain_ready = self.brain is not None and not _is_manually_offline()
        study = _match_study_command(text)
        if study and _tray_handles("study", brain_ready):
            self.entry.delete(0, "end")
            if not self.busy:
                self.close()
            if study == "study":
                _start_study_mode()
            else:
                _stop_study_mode()
            return "break"

        if _is_reminder_command(text) and _tray_handles("reminders", brain_ready):
            self.entry.delete(0, "end")
            if not self.busy:
                self.close()
            _show_reminder_card()
            return "break"

        if self.busy or self.brain is None:
            return "break"
        if _is_manually_offline():
            self.entry.delete(0, "end")
            self._add_system(_text("chat", "still_offline", online=_online_phrase()))
            return "break"

        self.entry.delete(0, "end")
        self._add_bubble("you", text)
        _stop_speaking()  # a new request cuts off earlier chat/voice playback
        self._speech_epoch = speech_control.capture()
        self._trace = voice_pipeline.LatencyTrace()
        self._trace.mark("input_end")  # typed: t = 0 is pressing send
        self._trace.extra["source"] = "tray_chat"
        self.messages = conversation.replay()
        self._turn_start = len(self.messages)
        self._turn_tail = []
        self.messages.append({"role": "user", "content": text})

        self.busy = True
        self.entry.config(state="normal")  # keep typed speech controls usable
        self._set_send_enabled(False)
        self.typing_label.config(text=_text("chat", "typing"))
        _set_thinking("chat", True)

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
                _text("confirm", "title"),
                _text("confirm", "question", tool=name, args=summary),
                parent=self.win,
            )
            done.set()

        root.after(0, ask)
        done.wait()
        return result.get("answer", False)

    def _respond_done(self, reply, err):
        if err:
            _set_thinking("chat", False)  # success clears it in _reveal_reply
        try:
            if err:
                self.typing_label.config(text="")
                self._add_system(_text("chat", "error", error=err))
                self._finish_turn()
                return
        except tk.TclError:
            return  # popup closed out from under this async callback -- fine

        # Append just this turn, re-reading the file first, so a reply here
        # can't erase what the wake-word listener recorded while the popup
        # sat open (see conversation.record).
        conversation.record(self._turn_tail)
        trace = getattr(self, "_trace", None)
        if trace is not None:
            trace.mark("first_token")  # brain.respond() isn't streamed: the whole reply
        # With local Kokoro the reply streams sentence by sentence and the
        # bubble is revealed as the first sentence starts playing. Otherwise
        # synthesizing (network, ~1-2s) AND opening the audio file for
        # playback (real MCI overhead -- see voice_output.py) both happen
        # silently in the background; the text bubble is revealed and
        # playback fired at the same instant, so only the unavoidable final
        # "play" command's own latency remains between them. With no voice
        # at all, reveal just happens almost instantly.
        threading.Thread(target=self._synthesize_then_reveal, args=(reply, self._speech_epoch, trace),
                         daemon=True).start()

    def _synthesize_then_reveal(self, reply: str, speech_epoch, trace=None):
        try:
            if self._stream_then_reveal(reply, speech_epoch, trace):
                return
            path = None if speech_control.cancelled(speech_epoch) else synthesize_inworld(reply)
            audio = prepare_audio(path) if path else None
            play_now = threading.Event() if audio is not None else None
            if audio is not None and trace is not None:
                trace.mark("tts_first_chunk")
                trace.extra["tts"] = "mci"
            root.after(0, lambda: self._reveal_reply(reply, audio, play_now))
            if audio is not None:
                # Windows' MCI ties an opened device to the thread that opened
                # it -- calling .play()/.close() from a different thread than
                # prepare_audio() ran on fails with "device is not open". So
                # this same thread waits here (blocking only itself, not the
                # UI) until the bubble reveal fires, then plays and closes.
                play_now.wait()
                try:
                    if trace is not None and not speech_control.cancelled(speech_epoch):
                        trace.mark("playback_start")
                    audio.play(speech_epoch=speech_epoch)
                finally:
                    audio.close()
        finally:
            if trace is not None and "tts" in trace.extra:
                trace.write()  # one line per spoken turn, logs/voice_latency.jsonl

    def _stream_then_reveal(self, reply: str, speech_epoch, trace) -> bool:
        """Streamed Kokoro (voice_output.speak_streaming). The bubble reveal
        fires once, from the speaker's own thread at first audio: it is
        scheduled on the Tk thread and the speaker waits for it before the
        first block is written, the same "reveal, then play" order the MCI
        path keeps. The stop button (speech_control) is checked between
        ~50 ms blocks. False = not handled; the caller uses the MCI path."""
        if not reply or speech_control.cancelled(speech_epoch):
            return False
        revealed = threading.Event()

        def reveal_at_first_audio():
            play_now = threading.Event()
            root.after(0, lambda: self._reveal_reply(reply, None, play_now))
            # Bounded, unlike the MCI path's wait: if Tk is gone, speak anyway.
            play_now.wait(REVEAL_WAIT_S)
            revealed.set()

        handled = speak_streaming(reply, should_stop=lambda: speech_control.cancelled(speech_epoch),
                                  on_start=reveal_at_first_audio, trace=trace)
        if handled and not revealed.is_set():
            # Stopped before the first sentence was ready: show the text anyway.
            root.after(0, lambda: self._reveal_reply(reply, None, None))
        return handled

    def _reveal_reply(self, reply: str, audio, play_now):
        try:
            self.typing_label.config(text="")
            if play_now is not None:
                play_now.set()  # let the waiting synth thread proceed to .play()
            if reply:
                self._add_bubble("denisse", reply)
            self._finish_turn()
        except tk.TclError:
            pass  # popup closed out from under this async callback -- fine
        finally:
            _show_reply_main(reply, source="chat" if _chat_popup is self else None)
            _set_thinking("chat", False)  # the reply has landed
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
            display = _clip(text, CONFIG["limits"]["heard_chars"])
            self.typing_label.config(text=_text("chat", "heard", text=display))
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
        step = _px(CONFIG["chat"]["glow_step"])
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
        self._glow_anim_job = self.win.after(CONFIG["timing"]["glow_frame_ms"],
                                             lambda: self._animate_side_glow(win_h))

    def close(self):
        global _chat_popup
        if self._anchor_job is not None:
            try:
                self.win.after_cancel(self._anchor_job)
            except tk.TclError:
                pass
        if self._glow_anim_job is not None:
            try:
                self.win.after_cancel(self._glow_anim_job)
            except Exception:
                pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if _chat_popup is self:
            _chat_popup = None

    def focus(self):
        self.win.lift()
        self.win.focus_force()
        self.entry.focus_set()


_chat_popup = None


def _open_chat(icon=None, item=None):
    def _do():
        global _chat_popup
        # Opening the chat means he's seen the reminder and moved on --
        # dismiss it rather than leave it beside the orb (asked 2026-10-07).
        if _reminder_card is not None:
            _reminder_card.close()  # its on_close clears _reminder_card
        pending = getattr(_reply_bubble, "full_message", None)
        _close_thinking_bubble()
        _close_reply_bubble()
        if _chat_popup is not None:
            _chat_popup.focus()
        else:
            _chat_popup = ChatPopup()
            latest = next((m.get("content", "") for m in reversed(_chat_popup.messages)
                           if m.get("role") == "assistant"), "")
            if isinstance(pending, str) and pending and pending != latest:
                _chat_popup._add_bubble("denisse", pending)

    root.after(0, _do)


_global_hotkey = None  # hotkey.GlobalHotkey once registered in main()


def _on_global_hotkey():
    """DENISSE_HOTKEY pressed (already on the Tk thread): open + focus the
    chat input from whatever app is in front, or fold it away if the chat
    is the window he's already typing in."""
    if _chat_popup is not None:
        try:
            if hotkey.is_foreground(_chat_popup.win.winfo_id()):
                _chat_popup.close()
                return
        except tk.TclError:
            pass
    _open_chat()

    def _focus_input():
        # A freshly built popup only focus_set()s its entry, which does
        # nothing while another app is in front -- force it here.
        if _chat_popup is not None:
            try:
                _chat_popup.focus()
            except tk.TclError:
                pass

    root.after(CONFIG["timing"]["hotkey_focus_delay_ms"], _focus_input)  # after _open_chat's root.after(0, ...)


_explain_hotkey = None  # explain_hotkey's GlobalHotkey once registered


def _on_explain_hotkey(req):
    """DENISSE_EXPLAIN_HOTKEY pressed. The selection was already read on
    the hotkey thread (explain_hotkey.grab) while the front app still had
    the focus; now open the chat and send "Explain this: ..." -- or show
    the one-line privacy note and send nothing."""
    _open_chat()

    def _deliver():
        popup = _chat_popup
        if popup is None:
            return
        try:
            popup.focus()
            if req.note:
                popup._add_system(req.note)
            elif req.message:
                explain_hotkey.send_when_ready(lambda: _chat_popup, req.message, root.after)
        except tk.TclError:
            pass

    root.after(CONFIG["timing"]["hotkey_focus_delay_ms"], _deliver)  # after _open_chat's root.after(0, ...)


def _toggle_pet_motion(icon=None, item=None):
    def toggle():
        if _floating_orb is not None:
            _floating_orb.motion_enabled = not _floating_orb.motion_enabled
    root.after(0, toggle)


def _build_menu(hotkey_label: str = "") -> pystray.Menu:
    """The tray icon's right-click menu; wording is tray.json text.menu."""
    m = CONFIG["text"]["menu"]
    return pystray.Menu(
        pystray.MenuItem(m["talk_with_hotkey"].format(hotkey=hotkey_label) if hotkey_label else m["talk"],
                         _open_chat, default=True),
        pystray.MenuItem(m["reminders"], _open_reminders),
        pystray.MenuItem(
            m["pet_movement"], _toggle_pet_motion,
            checked=lambda item: _floating_orb is not None and _floating_orb.motion_enabled,
        ),
        pystray.MenuItem(
            _study_menu_text, _toggle_study_mode, checked=lambda item: _study_mode is not None and _study_mode.active
        ),
        pystray.MenuItem(m["voice_console"], _open_voice_console),
        pystray.MenuItem(m["mission_control"], _open_mission_control),
        pystray.MenuItem(m["independent_work"], _toggle_independent_work,
                         checked=lambda item: _get_agent_engine().status()["enabled"]),
        pystray.MenuItem(m["agent_activity"], _show_agent_activity),
        pystray.MenuItem(
            m["organize"], _toggle_organize, checked=lambda item: _is_organizing(),
            enabled=lambda item: _get_agent_engine().status()["enabled"]
        ),
        pystray.MenuItem(
            m["optimize"], _toggle_optimize, checked=lambda item: _is_optimizing()
        ),
        pystray.MenuItem(
            m["wake_word"], _toggle_wake_word, checked=lambda item: _is_listening_for_wake_word()
        ),
        pystray.MenuItem(
            m["news"], _toggle_news_briefing, checked=lambda item: _is_news_briefing_active()
        ),
        pystray.MenuItem(m["project_folder"], _open_project_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(m["quit"], _quit),
    )


def main():
    global root
    root = tk.Tk()
    root.withdraw()  # only the popup shows -- no persistent main window
    _resolve_font_family()
    _resolve_dpi_scale()
    # Absorb Windows' one-time audio-subsystem cold-start cost now, in the
    # background, instead of on the first real reply (see voice_output.py).
    threading.Thread(target=warm_up_audio, daemon=True).start()

    # System-wide summon key (DENISSE_HOTKEY, default Ctrl+Alt+D; "off"
    # disables). A combo another app owns is logged and skipped.
    global _global_hotkey
    try:
        _global_hotkey = hotkey.start_from_env(_on_global_hotkey, lambda fn: root.after(0, fn))
    except Exception:
        _global_hotkey = None
    hotkey_label = hotkey.describe() if _global_hotkey is not None else ""
    # "Explain this" key (DENISSE_EXPLAIN_HOTKEY, default Ctrl+Alt+E): reads
    # the selection on its own thread, then opens and sends on Tk.
    global _explain_hotkey
    try:
        _explain_hotkey = explain_hotkey.start_from_env(
            _on_explain_hotkey, lambda fn: root.after(0, fn))
    except Exception:
        _explain_hotkey = None

    menu = _build_menu(hotkey_label)
    global _tray_icon
    text = CONFIG["text"]
    _tray_icon = pystray.Icon(
        text["icon_name"], _make_icon_image(),
        text["icon_title_with_hotkey"].format(hotkey=hotkey_label) if hotkey_label else text["icon_title"], menu,
    )
    _start_optimizing()
    if news.briefing_enabled():
        _start_news_briefing()

    # A job handed to Claude (delegate.py) finishes in the background a
    # minute or two later. Say so out of the orb, notify, and keep it in her
    # history so "where's that cover letter?" has an answer.
    import delegate

    def _delegate_done(msg):
        try:
            conversation.record([{"role": "assistant", "content": msg}])
        except Exception:
            pass
        _show_reply(msg)
        if _tray_icon is not None:
            try:
                _tray_icon.notify(msg[:CONFIG["limits"]["notify_chars"]], _text("notify", "title"))
            except Exception:
                pass

    delegate.on_done = _delegate_done

    global _floating_orb, _orb_controls
    _floating_orb = FloatingOrb(root, on_activate=_open_chat)
    _orb_controls = OrbControls(
        root, _floating_orb, on_chat=_open_chat,
        on_voice=lambda: _toggle_wake_word(None, None),
        on_reminders=_show_reminder_card,
        voice_active=lambda: _is_listening_for_wake_word() and not _wake_word_stop_event.is_set(),
        font_family=FONT_FAMILY,
    )
    # Right-click the orb for the reminder card (left-click stays chat).
    _floating_orb.canvas.bind("<Button-3>", lambda e: _show_reminder_card())
    global _study_mode
    _study_mode = StudyMode(
        root, _show_study_bubble, orb=_floating_orb, font_family=FONT_FAMILY,
        avoid=lambda: [b.win for b in (_reminder_card, _study_bubble, _reply_bubble, _thinking_bubble, _orb_controls) if b is not None],
        on_change=_on_study_change,
    )
    # The brain's study tools (study_mode.start_session etc.) run on its
    # worker thread; the clock lives here. Each call is marshalled onto the
    # Tk thread and waited for, so the tool result is what really happened.
    study_mode.set_dispatcher(_on_tk_thread)
    # Plain countdowns (timers.py) set by her set_timer tool. Its pill also
    # keeps off study mode's pill, so both can run at once.
    global _timer_display
    _timer_display = TimerDisplay(
        root, _floating_orb, _show_reply_main,
        notify=lambda title, text: _tray_icon.notify(text, title) if _tray_icon is not None else None,
        font_family=FONT_FAMILY,
        avoid=lambda: [b.win for b in (_reminder_card, _study_bubble, _reply_bubble, _thinking_bubble,
                                        _orb_controls,
                                        _study_mode.badge if _study_mode is not None else None) if b is not None],
    )

    # Nudge shortly after login, then keep checking so it also fires on a
    # machine that just stays on. Delayed so it doesn't fight the tray icon
    # and orb for the first seconds of startup.
    root.after(
        CONFIG["timing"]["startup_reminder_delay_ms"],
        lambda: threading.Thread(target=_remind_due_tasks, kwargs={"startup": True}, daemon=True).start(),
    )
    threading.Thread(target=_reminder_loop, daemon=True).start()
    threading.Thread(target=_unlock_watch_loop, daemon=True).start()
    _start_agent_workers()
    # What's running, for a restart decision made from outside the tray.
    import runtime_status
    runtime_status.start(_runtime_state)

    _tray_icon.run_detached()  # icon runs on its own thread; tkinter owns the main thread
    root.mainloop()


if __name__ == "__main__":
    main()
