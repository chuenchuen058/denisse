"""
What Chuen is looking at: the front window, the clipboard, the selection.

Roadmap section 4: "explain this", "rewrite this shorter", "what does this
error mean" used to reach a model that had no idea what "this" was. Two
cheap, local signals fix most of that:

  active_window()   title + process name of the foreground window
  clipboard_text()  CF_UNICODETEXT only -- never images or file lists
  selected_text()   the selection in the front app: snapshot the clipboard,
                    send Ctrl+C, read, put the old clipboard back exactly

and one tool built on them, look_at_this(), offered by tool_router only
when the request has a deictic cue ("this", "that", "selected",
"clipboard", "here") -- and, for a bare this/that/here, an action or a
question with it -- see has_cue().

ctypes only, no new dependencies. Nothing here is logged or written
anywhere: the text goes back to the local model as a tool result, and the
window title (context_line) goes into the system prompt for one call only.

Things it deliberately will not do:

  - read anything when the front window, or the text itself, looks like a
    password manager, a login form or a bank (PRIVATE). It says so instead.
  - send any key but Ctrl+C, and not even that when another modifier is
    held (the chord would be Ctrl+Alt+C, not a copy), when the front window
    is Denisse's own (it would copy her own input box), or when it is a
    terminal (Ctrl+C there with nothing selected interrupts the running
    program). Those cases fall back to the clipboard.
  - leave the clipboard changed. The old contents are restored whether the
    copy worked, timed out or raised.

What counts as a pointing request, which windows and text are private,
which apps are terminals, the size limits and every line of wording live
in desktop_context.json (AGENTS.md Rule 0), checked by json_config.py
against desktop_context.defaults.json; configure() reloads it.

Every Win32 call goes through a small `_w32_*` function so the tests can
replace them (scripts/test_desktop_context.py). Off Windows they all return
None and every public function degrades to "nothing found".
"""

import ctypes
import os
import re
import sys
import time

from pathlib import Path

import json_config

CONFIG_ENV_VAR = "DENISSE_DESKTOP_CONTEXT_CONFIG"
CONFIG_FILE = Path(__file__).with_name("desktop_context.json")
DEFAULTS_FILE = Path(__file__).with_name("desktop_context.defaults.json")

CF_UNICODETEXT = 13
# Clipboard formats whose data is a GDI handle, not an HGLOBAL, so it can't
# be copied as bytes. CF_BITMAP is synthesised again from CF_DIB (which is
# copied), so images still survive the round trip.
_GDI_FORMATS = {2, 3, 9, 14, 0x0080, 0x0082, 0x0083, 0x008E}

_NEVER = re.compile(r"(?!)")


def _any_of(patterns, words: bool = False):
    """One case-insensitive regex for a list of fragments; never matches if
    empty. ``words``: the fragments must stand as whole words."""
    if not patterns:
        return _NEVER
    body = "|".join(patterns)
    return re.compile(rf"\b(?:{body})\b" if words else body, re.IGNORECASE)


def _ask_regex(marks, words):
    """Any literal mark (a "?") or any whole ask word."""
    parts = [re.escape(m) for m in marks if m]
    if words:
        parts.append(r"\b(?:" + "|".join(words) + r")\b")
    return re.compile("|".join(parts), re.IGNORECASE) if parts else _NEVER


def _extra_checks(data: dict, defaults: dict, checker) -> dict:
    for section, keys in (("cues", ("strong", "weak", "ask_words")), ("privacy", ("window", "text"))):
        for key in keys:
            for frag in data[section][key]:
                try:
                    if not frag:
                        raise re.error("empty pattern")
                    re.compile(frag)
                except re.error as e:
                    data[section][key] = checker.bad(f"desktop_context.{section}.{key}",
                                                     f"has a bad pattern {frag!r} ({e})",
                                                     defaults[section][key])
                    break
    return data


CONFIG: dict = {}


def configure(path=None) -> dict:
    """(Re)load desktop_context.json (or ``path``, or
    $DENISSE_DESKTOP_CONTEXT_CONFIG) and rebind the module values from it."""
    global CONFIG, MAX_CHARS, SELECTION_BUDGET, _POLL, CUE, STRONG_CUE, ASK
    global PRIVATE, PRIVATE_TEXT, BLOCKED, _TERMINALS, _OFF_WORDS
    path = path or os.environ.get(CONFIG_ENV_VAR) or CONFIG_FILE
    CONFIG = json_config.load("desktop_context", path, DEFAULTS_FILE, extra=_extra_checks)
    lim, cues, priv = CONFIG["limits"], CONFIG["cues"], CONFIG["privacy"]
    MAX_CHARS = lim["max_chars"]
    SELECTION_BUDGET = lim["selection_budget_s"]
    _POLL = lim["poll_s"]
    # Requests that point at something on screen rather than describing it;
    # those that name the screen on their own; something asked of it.
    CUE = _any_of(cues["weak"], words=True)
    STRONG_CUE = _any_of(cues["strong"], words=True)
    ASK = _ask_regex(cues["ask_marks"], cues["ask_words"])
    # Title or process name of a window not to read; text that is a credential.
    PRIVATE = _any_of(priv["window"])
    PRIVATE_TEXT = _any_of(priv["text"])
    BLOCKED = CONFIG["wording"]["blocked"]
    _TERMINALS = frozenset(p.lower() for p in CONFIG["terminals"]["processes"])
    _OFF_WORDS = frozenset(w.strip().lower() for w in CONFIG["off_words"])
    return CONFIG


configure()


def enabled() -> bool:
    return os.environ.get("DENISSE_DESKTOP_CONTEXT", "1").strip().lower() not in _OFF_WORDS


def is_terminal(process: str) -> bool:
    return (process or "").lower() in _TERMINALS


def has_cue(text: str) -> bool:
    text = text or ""
    if STRONG_CUE.search(text):
        return True
    return bool(CUE.search(text) and ASK.search(text))


def is_private(*texts) -> bool:
    return any(PRIVATE.search(t or "") for t in texts)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


# --------------------------------------------------------------- Win32 layer

def _api():
    """(user32, kernel32) with 64-bit-safe signatures, or None off Windows."""
    global _API
    if _API is not None:
        return _API or None
    if sys.platform != "win32":
        _API = False
        return None
    from ctypes import wintypes as w
    u, k = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    sigs = [
        (u.GetForegroundWindow, [], w.HWND),
        (u.GetWindowTextLengthW, [w.HWND], ctypes.c_int),
        (u.GetWindowTextW, [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
        (u.GetWindowThreadProcessId, [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
        (u.OpenClipboard, [w.HWND], w.BOOL),
        (u.CloseClipboard, [], w.BOOL),
        (u.EmptyClipboard, [], w.BOOL),
        (u.EnumClipboardFormats, [ctypes.c_uint], ctypes.c_uint),
        (u.GetClipboardData, [ctypes.c_uint], w.HANDLE),
        (u.SetClipboardData, [ctypes.c_uint, w.HANDLE], w.HANDLE),
        (u.IsClipboardFormatAvailable, [ctypes.c_uint], w.BOOL),
        (u.GetClipboardSequenceNumber, [], w.DWORD),
        (u.GetAsyncKeyState, [ctypes.c_int], ctypes.c_short),
        (u.keybd_event, [w.BYTE, w.BYTE, w.DWORD, ctypes.c_void_p], None),
        (k.GlobalAlloc, [ctypes.c_uint, ctypes.c_size_t], w.HGLOBAL),
        (k.GlobalFree, [w.HGLOBAL], w.HGLOBAL),
        (k.GlobalLock, [w.HGLOBAL], ctypes.c_void_p),
        (k.GlobalUnlock, [w.HGLOBAL], w.BOOL),
        (k.GlobalSize, [w.HGLOBAL], ctypes.c_size_t),
        (k.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        (k.CloseHandle, [w.HANDLE], w.BOOL),
        (k.QueryFullProcessImageNameW, [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
    ]
    for fn, args, res in sigs:
        fn.argtypes, fn.restype = args, res
    _API = (u, k)
    return _API


_API = None


def _w32_foreground():
    """(title, pid) of the foreground window, or None."""
    api = _api()
    if not api:
        return None
    u, _k = api
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return None
    n = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    pid = ctypes.c_ulong(0)
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return buf.value, pid.value


def _w32_process_name(pid: int):
    api = _api()
    if not api or not pid:
        return None
    _u, k = api
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        return os.path.basename(buf.value)
    finally:
        k.CloseHandle(h)


def _open_clipboard(u) -> bool:
    # Another app may be holding it for a moment (a clipboard manager, the
    # app we just sent Ctrl+C to). A few short retries, not a long wait.
    lim = CONFIG["limits"]
    for _ in range(lim["clipboard_open_tries"]):
        if u.OpenClipboard(None):
            return True
        time.sleep(lim["clipboard_retry_s"])
    return False


def _w32_clipboard_seq():
    api = _api()
    return api[0].GetClipboardSequenceNumber() if api else None


def _w32_read_unicode():
    """CF_UNICODETEXT on the clipboard, or None. No other format is read."""
    api = _api()
    if not api:
        return None
    u, k = api
    if not u.IsClipboardFormatAvailable(CF_UNICODETEXT) or not _open_clipboard(u):
        return None
    try:
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = k.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def _w32_snapshot():
    """Every copyable format on the clipboard as [(format, bytes)], or None
    if the clipboard couldn't be opened (then nothing must be changed)."""
    api = _api()
    if not api:
        return None
    u, k = api
    if not _open_clipboard(u):
        return None
    items = []
    try:
        fmt = u.EnumClipboardFormats(0)
        while fmt:
            if fmt not in _GDI_FORMATS:
                h = u.GetClipboardData(fmt)
                if h:
                    size = k.GlobalSize(h)
                    p = k.GlobalLock(h) if size else None
                    if p:
                        try:
                            items.append((fmt, ctypes.string_at(p, size)))
                        finally:
                            k.GlobalUnlock(h)
            fmt = u.EnumClipboardFormats(fmt)
    finally:
        u.CloseClipboard()
    return items


def _w32_restore(items) -> bool:
    api = _api()
    if not api:
        return False
    u, k = api
    if not _open_clipboard(u):
        return False
    try:
        u.EmptyClipboard()
        for fmt, data in items:
            h = k.GlobalAlloc(0x0002, max(len(data), 1))  # GMEM_MOVEABLE
            if not h:
                continue
            p = k.GlobalLock(h)
            if not p:
                k.GlobalFree(h)
                continue
            ctypes.memmove(p, data, len(data))
            k.GlobalUnlock(h)
            if not u.SetClipboardData(fmt, h):
                k.GlobalFree(h)  # ownership only passes on success
        return True
    finally:
        u.CloseClipboard()


def _w32_modifiers_held() -> bool:
    """Shift, Alt or a Windows key physically down right now."""
    api = _api()
    if not api:
        return True
    u = api[0]
    return any(u.GetAsyncKeyState(vk) & 0x8000 for vk in (0x10, 0x12, 0x5B, 0x5C))


def _w32_send_ctrl_c() -> None:
    """Ctrl down, C down, C up, Ctrl up. The only keys this module sends."""
    api = _api()
    if not api:
        return
    u = api[0]
    VK_CONTROL, VK_C, KEYUP = 0x11, 0x43, 0x0002
    u.keybd_event(VK_CONTROL, 0, 0, None)
    u.keybd_event(VK_C, 0, 0, None)
    u.keybd_event(VK_C, 0, KEYUP, None)
    u.keybd_event(VK_CONTROL, 0, KEYUP, None)


# ------------------------------------------------------------- public API

def active_window():
    """{"title", "process"} of the foreground window, or None."""
    try:
        fg = _w32_foreground()
        if not fg:
            return None
        title, pid = fg
        return {"title": title or "", "process": _w32_process_name(pid) or "", "pid": pid}
    except Exception:
        return None


def clipboard_text(max_chars: int = None):
    """Current clipboard text (truncated to max_chars), or None."""
    max_chars = MAX_CHARS if max_chars is None else max_chars
    try:
        text = _w32_read_unicode()
    except Exception:
        return None
    if not text or not text.strip():
        return None
    return _truncate(text, max_chars)[0]


def selected_text(budget: float = None):
    """The selection in the front app, or None.

    Snapshot the clipboard, send Ctrl+C, wait (at most `budget` seconds)
    for the clipboard to change, read it, then put the snapshot back. If
    the clipboard never changed, nothing was selected and nothing is
    restored -- it was never touched.
    """
    budget = SELECTION_BUDGET if budget is None else budget
    try:
        if _w32_modifiers_held():
            return None
        snapshot = _w32_snapshot()
        if snapshot is None:
            return None  # can't promise to restore it, so don't touch it
        seq0 = _w32_clipboard_seq()
    except Exception:
        return None
    text = None
    try:
        _w32_send_ctrl_c()
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if _w32_clipboard_seq() != seq0:
                text = _w32_read_unicode()
                if text is not None:
                    break
            time.sleep(_POLL)
    except Exception:
        text = None
    finally:
        try:
            if _w32_clipboard_seq() != seq0:
                _w32_restore(snapshot)
        except Exception:
            pass
    return text if text and text.strip() else None


def _own_window(win) -> bool:
    pid = (win or {}).get("pid")
    return bool(pid) and pid in (os.getpid(), os.getppid())


def look_at_this() -> str:
    """The tool: selection, else clipboard, plus the front window's title."""
    say = CONFIG["wording"]
    if not enabled():
        return say["off"]
    win = active_window()
    title = (win or {}).get("title", "")
    proc = (win or {}).get("process", "")
    if is_private(title, proc):
        return BLOCKED
    own = _own_window(win)
    text, from_selection = None, False
    if win and not own and not is_terminal(proc):
        text, from_selection = selected_text(), True
    if not text:
        text, from_selection = _w32_safe_clipboard(), False
    if text and PRIVATE_TEXT.search(text):
        return BLOCKED
    # Text first, window last (desktop_context.json, wording._about says why).
    where = ""
    if win and not own and (title or proc):
        where = say["where"].format(
            title=title or say["untitled"],
            process=say["where_process"].format(process=proc) if proc else "")
    if not text:
        return "\n".join(filter(None, [say["nothing"], where]))
    text, cut = _truncate(text, MAX_CHARS)
    label = say["label_selection"] if from_selection else say["label_clipboard"]
    lines = [say["header"].format(label=label), say["open"], text, say["close"]]
    if cut:
        lines.append(say["cut"].format(max_chars=MAX_CHARS))
    if where:
        lines.append(where)
    lines.append(say["instruction"])
    return "\n".join(lines)


def _w32_safe_clipboard():
    try:
        return clipboard_text(MAX_CHARS * 2)  # look_at_this does the cut + note
    except Exception:
        return None


def context_line(request: str, max_words: int = None) -> str:
    """One short line naming the front window, for vague pointing requests
    ("what's this?", "fix that") only -- same cue rule as the tool. Empty
    for anything specific, private, or Denisse's own window."""
    if max_words is None:
        max_words = CONFIG["cues"]["context_line_max_words"]
    if not enabled() or not has_cue(request) or len((request or "").split()) > max_words:
        return ""
    win = active_window()
    if not win or _own_window(win) or is_private(win["title"], win["process"]):
        return ""
    title = " ".join(win["title"].split())[:CONFIG["limits"]["context_title_chars"]]
    if not title:
        return ""
    say = CONFIG["wording"]
    app = win["process"].rsplit(".", 1)[0]
    return say["context_line"].format(
        title=title, app=say["context_line_app"].format(app=app) if app else "")
