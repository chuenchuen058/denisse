"""Tests for explain_hotkey.py: the "explain this" hotkey.

Every Win32 call is mocked -- the desktop_context `_w32_*` layer, and
hotkey's user32/kernel32 (the fakes from test_hotkey.py) -- and the chat
window is a fake. Nothing is registered, copied or sent for real.
"""

import os
import sys
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import desktop_context as dc  # noqa: E402
import explain_hotkey as eh  # noqa: E402
import hotkey  # noqa: E402
from hotkey import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY, GlobalHotkey  # noqa: E402
from test_hotkey import FakeKernel32, FakeUser32  # noqa: E402

EDITOR = ("notes.txt - Notepad", 4321)


class Desktop:
    """Patches desktop_context's Win32 layer and records the order of
    events in a shared log, so tests can check grab-before-focus."""

    def __init__(self, events, window=EDITOR, process="notepad.exe",
                 selection=None, clipboard=None):
        self.events = events
        self.window = window
        self.process = process
        self.selection = selection
        self.clipboard = clipboard
        self.seq = 1
        self.ctrl_c = 0

    def _read(self):
        return self.clipboard

    def _send_ctrl_c(self):
        self.events.append("ctrl+c")
        self.ctrl_c += 1
        if self.selection is not None:
            self._saved = self.clipboard
            self.clipboard = self.selection
            self.seq += 1

    def _restore(self, items):
        self.clipboard = self._saved
        self.seq += 1
        return True

    def patches(self):
        return [
            mock.patch.object(dc, "_w32_foreground", lambda: self.window),
            mock.patch.object(dc, "_w32_process_name", lambda pid: self.process),
            mock.patch.object(dc, "_w32_modifiers_held", lambda: False),
            mock.patch.object(dc, "_w32_snapshot", lambda: [(13, b"x")]),
            mock.patch.object(dc, "_w32_clipboard_seq", lambda: self.seq),
            mock.patch.object(dc, "_w32_read_unicode", self._read),
            mock.patch.object(dc, "_w32_send_ctrl_c", self._send_ctrl_c),
            mock.patch.object(dc, "_w32_restore", self._restore),
            mock.patch.dict(os.environ, {"DENISSE_DESKTOP_CONTEXT": "1"}),
        ]

    def __enter__(self):
        self._p = self.patches()
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()


def no_wait():
    return True


class FakeEntry:
    def __init__(self):
        self.text = ""
        self.state = "disabled"

    def config(self, state=None):
        self.state = state

    def delete(self, a, b):
        self.text = ""

    def insert(self, i, text):
        self.text = text


class FakeChat:
    """Stands in for tray.ChatPopup: records what reaches it."""

    def __init__(self, events, brain=object()):
        self.events = events
        self.brain = brain
        self.busy = False
        self.entry = FakeEntry()
        self.sent = []
        self.notes = []

    def focus(self):
        self.events.append("focus")

    def _add_system(self, text):
        self.notes.append(text)

    def _send(self):
        self.sent.append(self.entry.text)
        self.events.append("send")


def tray_deliver(events, chat):
    """What tray._on_explain_hotkey does, against the fake chat."""

    def deliver(req):
        events.append("open")
        chat.focus()
        if req.note:
            chat._add_system(req.note)
        elif req.message:
            eh.send_when_ready(lambda: chat, req.message, lambda ms, fn: fn())

    return deliver


class PressTests(unittest.TestCase):
    def press(self, **desktop):
        self.events = []
        self.chat = FakeChat(self.events)
        with Desktop(self.events, **desktop) as self.desk:
            handler = eh.make_handler(tray_deliver(self.events, self.chat),
                                      schedule=lambda fn: fn(),
                                      grab_fn=lambda: eh.grab(wait=no_wait))
            handler()

    def test_selection_is_grabbed_before_the_chat_takes_focus(self):
        self.press(selection="def f(): return 1", clipboard="old clip")
        self.assertEqual(self.events, ["ctrl+c", "open", "focus", "send"])
        self.assertEqual(self.chat.sent, ["Explain this:\n\ndef f(): return 1"])
        self.assertEqual(self.desk.clipboard, "old clip")  # restored
        self.assertEqual(self.chat.notes, [])

    def test_selection_truncated_to_look_at_this_limit(self):
        self.press(selection="a" * (dc.MAX_CHARS + 500))
        msg = self.chat.sent[0]
        self.assertEqual(msg, "Explain this:\n\n" + "a" * dc.MAX_CHARS)

    def test_private_window_opens_chat_with_note_and_sends_nothing(self):
        self.press(window=("Bitwarden", 77), process="Bitwarden.exe",
                   selection="hunter2", clipboard="hunter2")
        self.assertEqual(self.desk.ctrl_c, 0)  # never even copied
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(self.chat.notes, [dc.BLOCKED])
        self.assertEqual(len(dc.BLOCKED.splitlines()), 1)
        self.assertIn("open", self.events)

    def test_sign_in_page_is_blocked(self):
        self.press(window=("Sign in - Google Accounts - Edge", 9), process="msedge.exe",
                   selection="me@example.com")
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(self.chat.notes, [dc.BLOCKED])

    def test_credential_text_is_blocked_whatever_the_window(self):
        self.press(selection="password: hunter2")
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(self.chat.notes, [dc.BLOCKED])
        self.assertNotIn("hunter2", " ".join(self.chat.notes))

    def test_empty_selection_falls_back_to_clipboard(self):
        self.press(selection=None, clipboard="ValueError: bad thing")
        self.assertEqual(self.desk.ctrl_c, 1)
        self.assertEqual(self.chat.sent, ["Explain this:\n\nValueError: bad thing"])

    def test_nothing_anywhere_just_opens_the_chat(self):
        self.press(selection=None, clipboard=None)
        self.assertEqual(self.events, ["ctrl+c", "open", "focus"])
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(self.chat.notes, [])

    def test_whitespace_only_counts_as_nothing(self):
        self.press(selection=None, clipboard="   \n ")
        self.assertEqual(self.chat.sent, [])

    def test_terminal_gets_no_ctrl_c(self):
        self.press(window=("PowerShell", 5), process="WindowsTerminal.exe",
                   selection="should not copy", clipboard="copied earlier")
        self.assertEqual(self.desk.ctrl_c, 0)
        self.assertEqual(self.chat.sent, ["Explain this:\n\ncopied earlier"])

    def test_own_window_gets_no_ctrl_c(self):
        self.press(window=("Denisse", os.getpid()), process="pythonw.exe",
                   selection="her own input", clipboard="clip")
        self.assertEqual(self.desk.ctrl_c, 0)
        self.assertEqual(self.chat.sent, ["Explain this:\n\nclip"])

    def test_desktop_context_off_reads_nothing(self):
        self.events = []
        chat = FakeChat(self.events)
        with Desktop(self.events, selection="x", clipboard="y") as desk, \
                mock.patch.dict(os.environ, {"DENISSE_DESKTOP_CONTEXT": "0"}):
            eh.make_handler(tray_deliver(self.events, chat), lambda fn: fn(),
                            grab_fn=lambda: eh.grab(wait=no_wait))()
        self.assertEqual(desk.ctrl_c, 0)
        self.assertEqual(chat.sent, [])
        self.assertEqual(chat.notes, [eh.OFF_NOTE])

    def test_waits_for_alt_to_come_up_before_copying(self):
        events = []
        held = iter([True, True, False])
        with Desktop(events, selection="text"):
            g = eh.grab(wait=lambda: eh.wait_for_release(
                held=lambda: (events.append("poll"), next(held))[1], sleep=lambda s: None))
        self.assertEqual(events, ["poll", "poll", "poll", "ctrl+c"])
        self.assertEqual(g, eh.Grab("text", "text", "selection", False))

    def test_wait_for_release_gives_up(self):
        self.assertFalse(eh.wait_for_release(held=lambda: True, timeout=0.05,
                                             sleep=lambda s: None))

    def test_grab_exception_still_opens_chat(self):
        events, logs = [], []
        chat = FakeChat(events)

        def boom():
            raise OSError("clipboard gone")

        eh.make_handler(tray_deliver(events, chat), lambda fn: fn(),
                        grab_fn=boom, log=logs.append)()
        self.assertEqual(events, ["open", "focus"])
        self.assertEqual(len(logs), 1)


class SendWhenReadyTests(unittest.TestCase):
    def test_waits_for_the_brain_then_sends(self):
        events = []
        chat = FakeChat(events, brain=None)
        pending = []
        eh.send_when_ready(lambda: chat, "Explain this:\n\nx",
                           lambda ms, fn: pending.append(fn), clock=lambda: 0.0)
        self.assertEqual(chat.sent, [])
        chat.brain = object()
        pending.pop()()
        self.assertEqual(chat.sent, ["Explain this:\n\nx"])

    def test_popup_closed_while_connecting_drops_it(self):
        chat = FakeChat([], brain=None)
        current = [chat]
        pending = []
        eh.send_when_ready(lambda: current[0], "m", lambda ms, fn: pending.append(fn),
                           clock=lambda: 0.0)
        current[0] = None
        chat.brain = object()
        pending.pop()()
        self.assertEqual(chat.sent, [])

    def test_never_connects_leaves_text_in_the_box(self):
        chat = FakeChat([], brain=None)
        t = [0.0]
        pending = []
        eh.send_when_ready(lambda: chat, "m", lambda ms, fn: pending.append(fn),
                           clock=lambda: t[0])
        t[0] = eh.SEND_WAIT + 1
        pending.pop()()
        self.assertEqual(pending, [])
        self.assertEqual(chat.sent, [])
        self.assertEqual(chat.entry.text, "m")
        self.assertEqual(chat.entry.state, "normal")


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(eh.ENV_VAR, None)
        os.environ.pop(hotkey.ENV_VAR, None)
        self.logs = []
        self.started = []

    def tearDown(self):
        for hk in self.started:
            hk.stop()
        self._env.stop()

    def start_explain(self, user32, summon_spec=None, grab_fn=None, deliver=None):
        hk = eh.start_from_env(deliver or (lambda req: None), lambda fn: fn(),
                               summon_spec=summon_spec, user32=user32,
                               kernel32=FakeKernel32(), log=self.logs.append,
                               grab_fn=grab_fn or (lambda: eh.Grab("empty")))
        if hk is not None:
            self.started.append(hk)
        return hk

    def test_default_is_ctrl_alt_e(self):
        user32 = FakeUser32()
        hk = self.start_explain(user32)
        self.assertIsNotNone(hk)
        _, id_, mods, vk, _ = user32.registered[0]
        self.assertEqual((mods, vk), (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("E")))
        self.assertEqual(id_, eh.HOTKEY_ID)
        self.assertEqual(eh.describe(), "Ctrl+Alt+E")

    def test_env_var_overrides(self):
        os.environ[eh.ENV_VAR] = "ctrl+shift+x"
        user32 = FakeUser32()
        self.assertIsNotNone(self.start_explain(user32))
        self.assertEqual(user32.registered[0][3], ord("X"))

    def test_off_switch_registers_nothing(self):
        for spec in ("off", "OFF", "none"):
            os.environ[eh.ENV_VAR] = spec
            user32 = FakeUser32()
            self.assertIsNone(self.start_explain(user32), spec)
            self.assertEqual(user32.registered, [])
        self.assertEqual(eh.describe(), "")
        self.assertEqual(self.logs, [])

    def test_bad_spec_logs_its_own_env_var(self):
        os.environ[eh.ENV_VAR] = "ctrl+wat"
        self.assertIsNone(self.start_explain(FakeUser32()))
        self.assertEqual(len(self.logs), 1)
        self.assertIn(eh.ENV_VAR, self.logs[0])

    def test_both_hotkeys_register_without_clashing(self):
        user32 = FakeUser32()
        summon = GlobalHotkey(lambda: None, None, lambda fn: fn(), user32=user32,
                              kernel32=FakeKernel32(), log=self.logs.append)
        self.assertTrue(summon.start())
        self.started.append(summon)
        explain = self.start_explain(user32)
        self.assertIsNotNone(explain)
        self.assertEqual(len(user32.registered), 2)
        combos = {(r[2], r[3]) for r in user32.registered}
        self.assertEqual(len(combos), 2)  # different keys
        ids = {r[1] for r in user32.registered}
        self.assertEqual(len(ids), 2)     # different ids
        threads = {r[4] for r in user32.registered}
        self.assertEqual(len(threads), 2)  # each on its own loop thread
        self.assertEqual(self.logs, [])

    def test_same_combo_as_summon_key_is_refused(self):
        os.environ[eh.ENV_VAR] = "ctrl+alt+d"
        user32 = FakeUser32()
        self.assertIsNone(self.start_explain(user32))
        self.assertEqual(user32.registered, [])
        self.assertEqual(len(self.logs), 1)
        os.environ[hotkey.ENV_VAR] = "ctrl+alt+e"
        os.environ.pop(eh.ENV_VAR)
        self.assertIsNone(self.start_explain(FakeUser32()))

    def test_summon_off_frees_its_combo(self):
        os.environ[hotkey.ENV_VAR] = "off"
        os.environ[eh.ENV_VAR] = "ctrl+alt+d"
        self.assertIsNotNone(self.start_explain(FakeUser32()))

    def test_press_grabs_on_the_hotkey_thread_then_schedules_ui(self):
        user32 = FakeUser32()
        seen = {}
        done = threading.Event()
        ui = []

        def grab_fn():
            seen["grab_thread"] = threading.get_ident()
            seen["order"] = ["grab"]
            return eh.Grab("text", "hello", "selection")

        def deliver(req):
            seen["order"].append("deliver")
            seen["req"] = req
            done.set()

        def schedule(fn):  # the Tk thread, here: just run it later
            ui.append(fn)
            fn()

        hk = eh.start_from_env(deliver, schedule, user32=user32, kernel32=FakeKernel32(),
                               log=self.logs.append, grab_fn=grab_fn)
        self.started.append(hk)
        reg_thread = user32.registered[0][4]
        user32.push(WM_HOTKEY, eh.HOTKEY_ID)
        self.assertTrue(done.wait(2))
        self.assertEqual(seen["grab_thread"], reg_thread)
        self.assertEqual(seen["order"], ["grab", "deliver"])
        self.assertEqual(seen["req"].message, "Explain this:\n\nhello")
        self.assertEqual(user32.allow_fg, 1)  # foreground right passed on


if __name__ == "__main__":
    unittest.main()
