"""System-wide hotkey for summoning Denisse from any application (Windows).

Uses Win32 ``RegisterHotKey`` through ctypes -- no extra dependency. The
hotkey lives on its own thread with its own message loop, because a hotkey
registered with ``hWnd=NULL`` is delivered as ``WM_HOTKEY`` to the message
queue of the thread that registered it, and must be unregistered by that
same thread. Each press is handed to ``schedule`` (in the tray that is
``root.after(0, fn)``) so all UI work stays on the Tk main thread.

Configuration: ``DENISSE_HOTKEY`` overrides ``summon.combo`` in hotkeys.json
(shipped as ``ctrl+alt+d``); any of its ``off_words`` disables it. The
modifier words, the "off" words and the timeouts live there too, checked by
json_config.py (bad value: one log line, the hotkeys.defaults.json value).
A combo another app already owns is logged once and ignored -- startup
never fails because of the hotkey.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path
from typing import Callable, Optional, Tuple

import json_config

ENV_VAR = "DENISSE_HOTKEY"
CONFIG_ENV_VAR = "DENISSE_HOTKEYS_CONFIG"
CONFIG_FILE = Path(__file__).with_name("hotkeys.json")
DEFAULTS_FILE = Path(__file__).with_name("hotkeys.defaults.json")

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # holding the combo down fires once, not on auto-repeat

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
ASFW_ANY = 0xFFFFFFFF

# The Win32 bit for each modifier hotkeys.json names; the words for them
# are data (``modifiers`` there).
_MOD_BITS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}

# Win32 virtual-key codes: a fixed table of the API, not a setting.
_NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pause": 0x13, "printscreen": 0x2C,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
for _i in range(1, 25):
    _NAMED_KEYS[f"f{_i}"] = 0x6F + _i  # F1 = 0x70 ... F24 = 0x87


def _key_vk(name: str) -> Optional[int]:
    if len(name) == 1 and ("a" <= name <= "z" or "0" <= name <= "9"):
        return ord(name.upper())  # VK codes for A-Z / 0-9 are their ASCII
    return _NAMED_KEYS.get(name)


# ------------------------------------------------------------ hotkeys.json

CONFIG: dict = {}
DEFAULT_HOTKEY = ""
_MODIFIERS: dict = {}
_OFF_WORDS: frozenset = frozenset()


def _modifier_table(words: dict) -> dict:
    table = {}
    for name, bit in _MOD_BITS.items():
        for w in words.get(name) or []:
            w = w.strip().lower()
            if not w or "+" in w or w in table:
                raise ValueError(f"modifier word {w!r} is empty, has '+' or is repeated")
            table[w] = bit
        if not words.get(name):
            raise ValueError(f"no words for {name!r}")
    return table


def _extra_checks(data: dict, defaults: dict, checker) -> dict:
    try:
        _modifier_table(data["modifiers"])
    except ValueError as e:
        data["modifiers"] = checker.bad("hotkeys.modifiers", f"is unusable ({e})", defaults["modifiers"])
    mods = _modifier_table(data["modifiers"])
    if not data["off_words"]:
        data["off_words"] = checker.bad("hotkeys.off_words", "is empty", defaults["off_words"])
    off = frozenset(w.strip().lower() for w in data["off_words"])
    for section in ("summon", "explain"):
        combo = data[section]["combo"]
        try:
            _parse(combo, mods, off)
        except ValueError as e:
            data[section]["combo"] = checker.bad(f"hotkeys.{section}.combo", f"doesn't parse ({e})",
                                                 defaults[section]["combo"])
    return data


def configure(path=None) -> dict:
    """(Re)load hotkeys.json (or ``path``, or $DENISSE_HOTKEYS_CONFIG)."""
    global CONFIG, DEFAULT_HOTKEY, _MODIFIERS, _OFF_WORDS
    path = path or os.environ.get(CONFIG_ENV_VAR) or CONFIG_FILE
    CONFIG = json_config.load("hotkeys", path, DEFAULTS_FILE, extra=_extra_checks, warn=_log)
    _MODIFIERS = _modifier_table(CONFIG["modifiers"])
    _OFF_WORDS = frozenset(w.strip().lower() for w in CONFIG["off_words"])
    DEFAULT_HOTKEY = CONFIG["summon"]["combo"]
    return CONFIG


def parse_hotkey(spec: Optional[str]) -> Optional[Tuple[int, int]]:
    """``"ctrl+alt+d"`` -> ``(modifiers, virtual_key)``; ``"off"`` -> None.

    Empty/None means the default. Case and spaces around ``+`` don't matter.
    Exactly one non-modifier key and at least one modifier are required --
    a bare system-wide key would swallow that key in every other app.
    Raises ValueError for anything it can't parse.
    """
    if spec is None or not spec.strip():
        spec = DEFAULT_HOTKEY
    return _parse(spec, _MODIFIERS, _OFF_WORDS)


def _parse(spec: str, modifiers: dict, off_words) -> Optional[Tuple[int, int]]:
    text = spec.strip().lower()
    if text in off_words:
        return None
    parts = [p.strip() for p in text.split("+")]
    # "ctrl+alt++" style: a trailing empty part means the key is "+" itself,
    # which has no stable VK across layouts -- reject rather than guess.
    if any(not p for p in parts):
        raise ValueError(f"bad hotkey {spec!r}: empty part")
    mods = 0
    key = None
    for part in parts:
        if part in modifiers:
            bit = modifiers[part]
            if mods & bit:
                raise ValueError(f"bad hotkey {spec!r}: {part!r} repeated")
            mods |= bit
            continue
        vk = _key_vk(part)
        if vk is None:
            raise ValueError(f"bad hotkey {spec!r}: unknown key {part!r}")
        if key is not None:
            raise ValueError(f"bad hotkey {spec!r}: more than one key")
        key = vk
    if key is None:
        raise ValueError(f"bad hotkey {spec!r}: no key, only modifiers")
    if not mods:
        raise ValueError(f"bad hotkey {spec!r}: needs one of {', '.join(_MOD_BITS)}")
    return mods, key


def _log(msg: str) -> None:
    # The tray runs under pythonw where stdout may be None; never let a log
    # line take the hotkey (or startup) down.
    try:
        print(f"[hotkey] {msg}", flush=True)
    except Exception:
        pass


configure()


class GlobalHotkey:
    """One registered hotkey on a dedicated message-loop thread.

    ``callback`` runs via ``schedule(callback)`` for every press. ``user32``
    and ``kernel32`` are injectable so tests can run without Win32.
    """

    HOTKEY_ID = 0xD15E  # any id unique within this thread

    def __init__(self, callback: Callable[[], None], spec: Optional[str],
                 schedule: Callable[[Callable[[], None]], None],
                 user32=None, kernel32=None, log: Callable[[str], None] = _log,
                 env_var: str = ENV_VAR, name: str = "denisse-hotkey",
                 hotkey_id: Optional[int] = None):
        # env_var/name/hotkey_id let a second hotkey (explain_hotkey.py)
        # reuse this class with its own setting name in the log lines.
        self.callback = callback
        self.spec = spec
        self.env_var = env_var
        self.name = name
        if hotkey_id is not None:
            self.HOTKEY_ID = hotkey_id
        self.schedule = schedule
        self._user32 = user32
        self._kernel32 = kernel32
        self._log = log
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._ready = threading.Event()
        self.registered = False

    def _libs(self):
        if self._user32 is None:
            self._user32 = ctypes.windll.user32
        if self._kernel32 is None:
            self._kernel32 = ctypes.windll.kernel32
        return self._user32, self._kernel32

    def start(self, timeout: Optional[float] = None) -> bool:
        """Start the thread and wait for registration. True if registered.

        Never raises: a bad spec, a non-Windows platform or a combo owned by
        another app is one log line and False.
        """
        try:
            parsed = parse_hotkey(self.spec)
        except ValueError as e:
            self._log(f"{e}; global hotkey disabled (set {self.env_var}, e.g. ctrl+alt+space)")
            return False
        if parsed is None:
            return False
        if self._user32 is None and sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._run, args=parsed,
                                        name=self.name, daemon=True)
        self._thread.start()
        self._ready.wait(CONFIG["summon"]["register_timeout_s"] if timeout is None else timeout)
        return self.registered

    def _run(self, mods: int, vk: int) -> None:
        try:
            user32, kernel32 = self._libs()
            self._thread_id = kernel32.GetCurrentThreadId()
            if not user32.RegisterHotKey(None, self.HOTKEY_ID, mods | MOD_NOREPEAT, vk):
                self._log(f"could not register {self.spec or DEFAULT_HOTKEY!r} "
                          f"(another app probably owns it); global hotkey disabled")
                return
            self.registered = True
        except Exception as e:
            self._log(f"hotkey setup failed: {e}; global hotkey disabled")
            return
        finally:
            self._ready.set()
        try:
            self._loop(user32)
        finally:
            try:
                user32.UnregisterHotKey(None, self.HOTKEY_ID)
            except Exception:
                pass
            self.registered = False

    def _loop(self, user32) -> None:
        from ctypes import wintypes
        msg = wintypes.MSG()
        while True:
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got == 0 or got == -1:  # WM_QUIT, or an error
                return
            if msg.message == WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                self._on_press(user32)

    def _on_press(self, user32) -> None:
        # Receiving the hotkey earns this process the right to take the
        # foreground; pass it on so the Tk thread's focus_force() works
        # even when another app is in front.
        try:
            user32.AllowSetForegroundWindow(ASFW_ANY)
        except Exception:
            pass
        try:
            self.schedule(self.callback)
        except Exception as e:
            self._log(f"hotkey callback could not be scheduled: {e}")

    def stop(self, timeout: float = 2.0) -> None:
        """Quit the message loop; the thread unregisters on its way out."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        if self._thread_id is not None:
            try:
                self._libs()[0].PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        thread.join(timeout)


GA_ROOT = 2


def is_foreground(hwnd: int, user32=None) -> bool:
    """True if the top-level window containing ``hwnd`` is the active one.

    Tk's ``winfo_id()`` is a child of the real top-level, hence GetAncestor.
    """
    try:
        if user32 is None:
            user32 = ctypes.windll.user32
        fg = user32.GetForegroundWindow()
        return bool(fg) and fg == user32.GetAncestor(hwnd, GA_ROOT)
    except Exception:
        return False


def start_from_env(callback: Callable[[], None],
                   schedule: Callable[[Callable[[], None]], None]) -> Optional[GlobalHotkey]:
    """Register ``DENISSE_HOTKEY`` (or the default). None if off/failed."""
    hk = GlobalHotkey(callback, os.environ.get(ENV_VAR), schedule)
    return hk if hk.start() else None


def describe(spec: Optional[str] = None) -> str:
    """Human label for menus: ``"Ctrl+Alt+D"``, or "" when disabled/invalid."""
    if spec is None:
        spec = os.environ.get(ENV_VAR)
    try:
        if parse_hotkey(spec) is None:
            return ""
    except ValueError:
        return ""
    text = (spec or DEFAULT_HOTKEY).strip()
    return "+".join(p.strip().capitalize() if len(p.strip()) > 1 else p.strip().upper()
                    for p in text.split("+"))
