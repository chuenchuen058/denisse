"""Tests for hotkey.py: string parsing and the registration thread, with
every Win32 call mocked (no real hotkey is registered)."""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import hotkey  # noqa: E402
from hotkey import (MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, MOD_SHIFT, MOD_WIN,  # noqa: E402
                    WM_HOTKEY, WM_QUIT, GlobalHotkey, parse_hotkey)


class ParseHotkeyTests(unittest.TestCase):
    def test_default(self):
        self.assertEqual(parse_hotkey("ctrl+alt+d"), (MOD_CONTROL | MOD_ALT, ord("D")))
        self.assertEqual(parse_hotkey(None), (MOD_CONTROL | MOD_ALT, ord("D")))
        self.assertEqual(parse_hotkey("   "), (MOD_CONTROL | MOD_ALT, ord("D")))

    def test_case_and_spaces(self):
        self.assertEqual(parse_hotkey(" Ctrl + ALT + Space "), (MOD_CONTROL | MOD_ALT, 0x20))

    def test_modifier_aliases(self):
        self.assertEqual(parse_hotkey("control+shift+win+k"),
                         (MOD_CONTROL | MOD_SHIFT | MOD_WIN, ord("K")))
        self.assertEqual(parse_hotkey("super+1"), (MOD_WIN, ord("1")))

    def test_keys(self):
        self.assertEqual(parse_hotkey("alt+f1")[1], 0x70)
        self.assertEqual(parse_hotkey("alt+f12")[1], 0x7B)
        self.assertEqual(parse_hotkey("alt+f24")[1], 0x87)
        self.assertEqual(parse_hotkey("ctrl+enter")[1], 0x0D)
        self.assertEqual(parse_hotkey("ctrl+9")[1], ord("9"))
        self.assertEqual(parse_hotkey("ctrl+`")[1], 0xC0)

    def test_off(self):
        for s in ("off", "OFF", " Off ", "none", "disabled"):
            self.assertIsNone(parse_hotkey(s), s)

    def test_bad_strings(self):
        for s in ("ctrl+alt", "d", "ctrl+alt+d+e", "ctrl+bogus", "ctrl++",
                  "+d", "ctrl+ctrl+d", "hyper+d", "ctrl+f25", "ctrl+alt+dd"):
            with self.assertRaises(ValueError, msg=s):
                parse_hotkey(s)

    def test_describe(self):
        self.assertEqual(hotkey.describe("ctrl+alt+d"), "Ctrl+Alt+D")
        self.assertEqual(hotkey.describe("ctrl+alt+space"), "Ctrl+Alt+Space")
        self.assertEqual(hotkey.describe("off"), "")
        self.assertEqual(hotkey.describe("ctrl+nope"), "")


class FakeUser32:
    """Stands in for ctypes.windll.user32: a scripted message queue."""

    def __init__(self, register_ok=True):
        self.register_ok = register_ok
        self.registered = []
        self.unregistered = []
        self.posted = []
        self.allow_fg = 0
        self._queue = []
        self._cv = threading.Condition()

    def RegisterHotKey(self, hwnd, id_, mods, vk):
        self.registered.append((hwnd, id_, mods, vk, threading.get_ident()))
        return 1 if self.register_ok else 0

    def UnregisterHotKey(self, hwnd, id_):
        self.unregistered.append((hwnd, id_, threading.get_ident()))
        return 1

    def AllowSetForegroundWindow(self, pid):
        self.allow_fg += 1
        return 1

    def push(self, message, wparam=0):
        with self._cv:
            self._queue.append((message, wparam))
            self._cv.notify_all()

    def PostThreadMessageW(self, tid, message, wparam, lparam):
        self.posted.append((tid, message))
        self.push(message, wparam)
        return 1

    def GetMessageW(self, pmsg, hwnd, lo, hi):
        with self._cv:
            if not self._cv.wait_for(lambda: self._queue, timeout=5):
                return 0  # never hang the test suite
            message, wparam = self._queue.pop(0)
        msg = pmsg._obj
        msg.message = message
        msg.wParam = wparam
        return 0 if message == WM_QUIT else 1


class FakeKernel32:
    def GetCurrentThreadId(self):
        return 4242


class GlobalHotkeyTests(unittest.TestCase):
    def make(self, spec="ctrl+alt+d", register_ok=True):
        self.user32 = FakeUser32(register_ok)
        self.logs = []
        self.presses = []
        self.scheduled = []

        def schedule(fn):
            self.scheduled.append(fn)
            fn()

        return GlobalHotkey(lambda: self.presses.append(1), spec, schedule,
                            user32=self.user32, kernel32=FakeKernel32(),
                            log=self.logs.append)

    def test_register_press_and_unregister_on_stop(self):
        hk = self.make()
        self.assertTrue(hk.start())
        self.assertTrue(hk.registered)
        _, id_, mods, vk, reg_thread = self.user32.registered[0]
        self.assertEqual(mods, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT)
        self.assertEqual(vk, ord("D"))
        self.assertNotEqual(reg_thread, threading.get_ident())  # own thread

        self.user32.push(WM_HOTKEY, id_)
        self.user32.push(WM_HOTKEY, 999)  # someone else's id: ignored
        self.user32.push(WM_HOTKEY, id_)
        hk.stop()

        self.assertEqual(len(self.presses), 2)
        self.assertEqual(self.user32.allow_fg, 2)
        self.assertEqual(self.user32.posted, [(4242, WM_QUIT)])
        self.assertFalse(hk._thread.is_alive())
        self.assertEqual(len(self.user32.unregistered), 1)
        # Unregistered from the same thread that registered it.
        self.assertEqual(self.user32.unregistered[0][2], reg_thread)
        self.assertFalse(hk.registered)
        self.assertEqual(self.logs, [])

    def test_registration_failure_logs_once_and_returns(self):
        hk = self.make(register_ok=False)
        self.assertFalse(hk.start())
        hk._thread.join(2)
        self.assertFalse(hk._thread.is_alive())
        self.assertEqual(len(self.logs), 1)
        self.assertIn("ctrl+alt+d", self.logs[0])
        self.assertEqual(self.user32.unregistered, [])
        hk.stop()  # harmless when nothing is running

    def test_off_registers_nothing(self):
        hk = self.make(spec="off")
        self.assertFalse(hk.start())
        self.assertIsNone(hk._thread)
        self.assertEqual(self.user32.registered, [])
        self.assertEqual(self.logs, [])

    def test_bad_spec_logs_and_does_not_raise(self):
        hk = self.make(spec="ctrl+wat")
        self.assertFalse(hk.start())
        self.assertEqual(self.user32.registered, [])
        self.assertEqual(len(self.logs), 1)

    def test_win32_exception_does_not_escape(self):
        hk = self.make()

        def boom(*a):
            raise OSError("no user32")

        self.user32.RegisterHotKey = boom
        self.assertFalse(hk.start())
        self.assertEqual(len(self.logs), 1)

    def test_schedule_failure_is_logged_not_fatal(self):
        hk = self.make()

        def bad_schedule(fn):
            raise RuntimeError("main loop gone")

        hk.schedule = bad_schedule
        self.assertTrue(hk.start())
        self.user32.push(WM_HOTKEY, GlobalHotkey.HOTKEY_ID)
        hk.stop()
        self.assertEqual(len(self.logs), 1)
        self.assertEqual(len(self.user32.unregistered), 1)

    def test_start_from_env(self):
        old = os.environ.get(hotkey.ENV_VAR)
        try:
            os.environ[hotkey.ENV_VAR] = "off"
            self.assertIsNone(hotkey.start_from_env(lambda: None, lambda fn: fn()))
        finally:
            if old is None:
                os.environ.pop(hotkey.ENV_VAR, None)
            else:
                os.environ[hotkey.ENV_VAR] = old


class IsForegroundTests(unittest.TestCase):
    class U:
        def __init__(self, fg, root):
            self.fg, self.root = fg, root

        def GetForegroundWindow(self):
            return self.fg

        def GetAncestor(self, hwnd, flag):
            return self.root

    def test_foreground(self):
        self.assertTrue(hotkey.is_foreground(5, self.U(10, 10)))
        self.assertFalse(hotkey.is_foreground(5, self.U(11, 10)))
        self.assertFalse(hotkey.is_foreground(5, self.U(0, 0)))


def _write_config(test, edit):
    import json
    import tempfile
    with open(hotkey.CONFIG_FILE, encoding="utf-8") as f:
        data = json.load(f)
    edit(data)
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    test.addCleanup(os.remove, path)
    return path


class ConfigData(unittest.TestCase):
    """hotkeys.json drives the combos and words: change the data, change the behaviour."""

    def tearDown(self):
        hotkey.configure(hotkey.CONFIG_FILE)

    def test_shipped_file_equals_defaults(self):
        import json
        strip = lambda d: {k: strip(v) for k, v in d.items() if not k.startswith("_")} \
            if isinstance(d, dict) else d
        with open(hotkey.CONFIG_FILE, encoding="utf-8") as a, \
                open(hotkey.DEFAULTS_FILE, encoding="utf-8") as b:
            self.assertEqual(strip(json.load(a)), strip(json.load(b)))

    def test_combo_modifier_and_off_words_from_the_data(self):
        def edit(d):
            d["summon"]["combo"] = "cmd+shift+k"
            d["modifiers"]["win"].append("cmd")
            d["off_words"].append("nope")
        hotkey.configure(_write_config(self, edit))
        self.assertEqual(parse_hotkey(None), (MOD_WIN | MOD_SHIFT, ord("K")))
        self.assertIsNone(parse_hotkey("nope"))
        self.assertEqual(hotkey.describe("cmd+shift+k"), "Cmd+Shift+K")

    def test_bad_values_fall_back_with_a_log_line(self):
        def edit(d):
            d["summon"]["combo"] = "ctrl+bogus"
            d["modifiers"]["alt"] = []
            d["summon"]["register_timeout_s"] = -1
        from unittest import mock
        lines = []
        with mock.patch.object(hotkey, "_log", lines.append):
            hotkey.configure(_write_config(self, edit))
        text = "\n".join(lines)
        for key in ("summon.combo", "modifiers", "register_timeout_s"):
            self.assertIn(key, text)
        self.assertEqual(parse_hotkey(None), (MOD_CONTROL | MOD_ALT, ord("D")))
        self.assertEqual(hotkey.CONFIG["summon"]["register_timeout_s"], 2.0)


if __name__ == "__main__":
    unittest.main()
