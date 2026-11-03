"""One key for "explain this": send the front app's selection to Denisse.

A second global hotkey (default Ctrl+Alt+E, ``DENISSE_EXPLAIN_HOTKEY``,
``off`` disables) built from the two pieces already there:

  hotkey.GlobalHotkey          registration, parsing, its own message loop
  desktop_context              the selection / clipboard / privacy rules

Order matters. The selection is read *on the hotkey thread, before
anything is scheduled on Tk*, so the app in front still owns the
selection when Ctrl+C goes to it. Only then is the chat opened (which
takes the focus away) with "Explain this:" and the text, and sent.

What a press does:

  privacy block    front window or the text looks like a password manager,
                   sign-in page or bank -> chat opens with a one-line note,
                   nothing is sent and the text is never shown
  selection        "Explain this:" + selection (cut to MAX_CHARS), sent
  no selection     same with the clipboard text
  nothing at all   the chat just opens, as Ctrl+Alt+D would
  reading off      DENISSE_DESKTOP_CONTEXT=0 -> chat opens with a note

The press arrives while Ctrl+Alt are still down, and
desktop_context.selected_text() refuses to send Ctrl+C while Alt/Shift/Win
are held (the chord would be Ctrl+Alt+C). So the grab first waits, briefly,
for those keys to come up.
"""

from __future__ import annotations

import os
import time
from typing import Callable, NamedTuple, Optional

import desktop_context
import hotkey

DEFAULT_HOTKEY = "ctrl+alt+e"
ENV_VAR = "DENISSE_EXPLAIN_HOTKEY"
HOTKEY_ID = 0xD15F  # differs from hotkey.GlobalHotkey.HOTKEY_ID
PREFIX = "Explain this:"
RELEASE_WAIT = 1.0  # seconds to wait for Alt/Shift/Win to come up
OFF_NOTE = "Reading the screen is switched off (DENISSE_DESKTOP_CONTEXT=0), so I didn't look."


class Grab(NamedTuple):
    kind: str            # "text" | "blocked" | "empty" | "off"
    text: str = ""
    source: str = ""     # "selection" | "clipboard"
    cut: bool = False


class Request(NamedTuple):
    message: Optional[str]  # to send, or None
    note: Optional[str]     # one line to show instead, or None


def resolve_spec(spec: Optional[str] = None) -> str:
    """The configured combo: ``spec``/env var, or the default when empty."""
    if spec is None:
        spec = os.environ.get(ENV_VAR)
    return spec if spec and spec.strip() else DEFAULT_HOTKEY


def wait_for_release(held: Callable[[], bool] = None, timeout: float = RELEASE_WAIT,
                     sleep: Callable[[float], None] = time.sleep) -> bool:
    """Poll until Alt/Shift/Win are up. True if released in time."""
    held = held or desktop_context._w32_modifiers_held
    deadline = time.monotonic() + timeout
    while True:
        try:
            if not held():
                return True
        except Exception:
            return False
        if time.monotonic() >= deadline:
            return False
        sleep(0.02)


def grab(wait: Callable[[], bool] = wait_for_release) -> Grab:
    """Read what's selected in the front app (else the clipboard).

    Same rules as desktop_context.look_at_this(): private windows and
    credential-looking text are blocked, Denisse's own window and terminals
    get no Ctrl+C (clipboard only).
    """
    dc = desktop_context
    if not dc.enabled():
        return Grab("off")
    win = dc.active_window()
    title = (win or {}).get("title", "")
    proc = (win or {}).get("process", "")
    if dc.is_private(title, proc):
        return Grab("blocked")
    text, source = None, ""
    if win and not dc._own_window(win) and proc.lower() not in dc._TERMINALS:
        wait()
        text, source = dc.selected_text(), "selection"
    if not text:
        text, source = dc.clipboard_text(dc.MAX_CHARS * 2), "clipboard"
    if text and dc.PRIVATE_TEXT.search(text):
        return Grab("blocked")
    if not text or not text.strip():
        return Grab("empty")
    text, cut = dc._truncate(text.strip(), dc.MAX_CHARS)
    return Grab("text", text, source, cut)


def build_request(g: Grab) -> Request:
    if g.kind == "blocked":
        return Request(None, desktop_context.BLOCKED)
    if g.kind == "off":
        return Request(None, OFF_NOTE)
    if g.kind != "text":
        return Request(None, None)
    return Request(f"{PREFIX}\n\n{g.text}", None)


def make_handler(deliver: Callable[[Request], None],
                 schedule: Callable[[Callable[[], None]], None],
                 grab_fn: Callable[[], Grab] = None,
                 log: Callable[[str], None] = hotkey._log) -> Callable[[], None]:
    """The hotkey callback. Runs on the hotkey thread: grab first, then
    schedule ``deliver(request)`` on the UI thread (which opens the chat)."""
    grab_fn = grab_fn or grab

    def on_press() -> None:
        try:
            g = grab_fn()
        except Exception as e:
            log(f"explain hotkey could not read the selection: {e}")
            g = Grab("empty")
        req = build_request(g)
        try:
            schedule(lambda: deliver(req))
        except Exception as e:
            log(f"explain hotkey could not open the chat: {e}")

    return on_press


SEND_WAIT = 30.0  # seconds a fresh chat may take to connect to the brain


def send_when_ready(get_popup: Callable[[], object], text: str,
                    after: Callable[[int, Callable[[], None]], None],
                    deadline: Optional[float] = None,
                    clock: Callable[[], float] = time.monotonic) -> None:
    """Put ``text`` in the chat's input and send it once the chat can send.

    A freshly opened chat connects to the brain in the background and its
    send is a no-op until then, so retry every 200 ms. If the popup closes
    meanwhile, drop it; if it never connects in SEND_WAIT, leave the text
    in the box for him to send by hand. Runs on the Tk thread.
    """
    if deadline is None:
        deadline = clock() + SEND_WAIT
    popup = get_popup()
    if popup is None:
        return
    ready = getattr(popup, "brain", None) is not None and not getattr(popup, "busy", False)
    if not ready and clock() < deadline:
        after(200, lambda: _retry(get_popup, popup, text, after, deadline, clock))
        return
    entry = popup.entry
    entry.config(state="normal")
    entry.delete(0, "end")
    entry.insert(0, text)
    if ready:
        popup._send()


def _retry(get_popup, popup, text, after, deadline, clock):
    if get_popup() is not popup:
        return  # closed (or replaced) while connecting
    send_when_ready(get_popup, text, after, deadline, clock)


def clashes(spec: Optional[str], other: Optional[str]) -> bool:
    """True if two specs name the same key combination."""
    try:
        a, b = hotkey.parse_hotkey(spec), hotkey.parse_hotkey(other)
    except ValueError:
        return False
    return a is not None and a == b


def start_from_env(deliver: Callable[[Request], None],
                   schedule: Callable[[Callable[[], None]], None],
                   summon_spec: Optional[str] = None,
                   user32=None, kernel32=None,
                   log: Callable[[str], None] = hotkey._log,
                   grab_fn: Callable[[], Grab] = None) -> Optional[hotkey.GlobalHotkey]:
    """Register the explain hotkey. None if off, invalid, taken, or the
    same combo as the summon hotkey (``summon_spec``, default
    ``DENISSE_HOTKEY``)."""
    spec = resolve_spec()
    if summon_spec is None:
        summon_spec = os.environ.get(hotkey.ENV_VAR)
    if clashes(spec, summon_spec):
        log(f"{ENV_VAR}={spec!r} is the same combo as {hotkey.ENV_VAR}; "
            f"explain hotkey disabled")
        return None
    handler = make_handler(deliver, schedule, grab_fn=grab_fn, log=log)
    # The handler is called directly on the hotkey thread (schedule=call),
    # so the grab happens before the chat takes the focus.
    hk = hotkey.GlobalHotkey(handler, spec, lambda fn: fn(),
                             user32=user32, kernel32=kernel32, log=log,
                             env_var=ENV_VAR, name="denisse-explain-hotkey",
                             hotkey_id=HOTKEY_ID)
    return hk if hk.start() else None


def describe() -> str:
    """Menu label, e.g. "Ctrl+Alt+E"; "" when off or invalid."""
    return hotkey.describe(resolve_spec())
