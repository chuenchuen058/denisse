"""desktop_context with every Win32 call mocked: no real clipboard, no keys sent."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_context as dc
import tool_router


class FakeDesktop:
    """A clipboard, a front window and a keyboard, all in memory."""

    def __init__(self, clipboard=None, selection=None, title="notes.txt - Notepad",
                 process="notepad.exe", pid=4242, copy_raises=False, modifiers=False,
                 copy_lands=True):
        self.formats = [(13, (clipboard or "").encode("utf-16-le") + b"\0\0")] if clipboard is not None else []
        self.selection = selection
        self.title, self.process, self.pid = title, process, pid
        self.seq = 1
        self.keys = []
        self.restored = 0
        self.copy_raises = copy_raises
        self.modifiers = modifiers
        self.copy_lands = copy_lands

    def text(self):
        for fmt, data in self.formats:
            if fmt == 13:
                return data.decode("utf-16-le").rstrip("\0")
        return None

    def send_ctrl_c(self):
        self.keys.append("ctrl+c")
        if self.copy_raises:
            self.seq += 1           # the app emptied the clipboard, then we fail
            self.formats = []
            raise OSError("boom")
        if self.selection is not None and self.copy_lands:
            self.seq += 1
            self.formats = [(13, self.selection.encode("utf-16-le") + b"\0\0")]

    def restore(self, items):
        self.restored += 1
        self.seq += 1
        self.formats = list(items)
        return True

    def patches(self):
        return [
            patch.object(dc, "_w32_foreground", lambda: (self.title, self.pid)),
            patch.object(dc, "_w32_process_name", lambda pid: self.process),
            patch.object(dc, "_w32_clipboard_seq", lambda: self.seq),
            patch.object(dc, "_w32_read_unicode", self.text),
            patch.object(dc, "_w32_snapshot", lambda: list(self.formats)),
            patch.object(dc, "_w32_restore", self.restore),
            patch.object(dc, "_w32_send_ctrl_c", self.send_ctrl_c),
            patch.object(dc, "_w32_modifiers_held", lambda: self.modifiers),
            patch.dict(os.environ, {"DENISSE_DESKTOP_CONTEXT": "1"}),
        ]

    def __enter__(self):
        self._p = self.patches()
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()


class ClipboardRestore(unittest.TestCase):
    def test_selection_read_and_old_clipboard_restored_exactly(self):
        with FakeDesktop(clipboard="old stuff", selection="ZeroDivisionError") as d:
            before = list(d.formats)
            self.assertEqual(dc.selected_text(budget=0.1), "ZeroDivisionError")
            self.assertEqual(d.formats, before)
            self.assertEqual(d.restored, 1)
            self.assertEqual(d.keys, ["ctrl+c"])

    def test_restored_when_the_copy_fails_midway(self):
        with FakeDesktop(clipboard="old stuff", copy_raises=True) as d:
            before = list(d.formats)
            self.assertIsNone(dc.selected_text(budget=0.1))
            self.assertEqual(d.formats, before)
            self.assertEqual(d.restored, 1)

    def test_nothing_selected_times_out_and_leaves_clipboard_untouched(self):
        with FakeDesktop(clipboard="old stuff", selection=None) as d:
            self.assertIsNone(dc.selected_text(budget=0.05))
            self.assertEqual(d.text(), "old stuff")
            self.assertEqual(d.restored, 0)  # never changed, so never rewritten

    def test_time_boxed(self):
        import time
        with FakeDesktop(clipboard="x", selection=None):
            t = time.monotonic()
            dc.selected_text()
            self.assertLess(time.monotonic() - t, 0.6)

    def test_empty_clipboard_is_restored_as_empty(self):
        with FakeDesktop(clipboard=None, selection="hello") as d:
            self.assertEqual(dc.selected_text(budget=0.1), "hello")
            self.assertEqual(d.formats, [])

    def test_unopenable_clipboard_means_no_keys_sent(self):
        with FakeDesktop(clipboard="old", selection="sel") as d, \
                patch.object(dc, "_w32_snapshot", lambda: None):
            self.assertIsNone(dc.selected_text(budget=0.1))
            self.assertEqual(d.keys, [])

    def test_held_modifier_means_no_keys_sent(self):
        with FakeDesktop(clipboard="old", selection="sel", modifiers=True) as d:
            self.assertIsNone(dc.selected_text(budget=0.1))
            self.assertEqual(d.keys, [])

    def test_no_ctrl_c_into_terminal_or_own_window(self):
        for proc, pid in [("WindowsTerminal.exe", 4242), ("pythonw.exe", os.getpid())]:
            with FakeDesktop(clipboard="from clipboard", selection="sel", process=proc, pid=pid) as d:
                out = dc.look_at_this()
                self.assertEqual(d.keys, [], proc)
                self.assertIn("from clipboard", out)

    def test_off_windows_everything_is_none(self):
        with patch.object(dc, "_api", lambda: None):
            self.assertIsNone(dc.active_window())
            self.assertIsNone(dc.clipboard_text())
            self.assertIsNone(dc.selected_text(budget=0.05))


class LookAtThis(unittest.TestCase):
    def test_selection_first_with_window(self):
        with FakeDesktop(clipboard="clip", selection="KeyError: 'user_id'") as d:
            out = dc.look_at_this()
            self.assertIn("KeyError: 'user_id'", out)
            self.assertNotIn("clip\n", out)
            self.assertIn("notes.txt - Notepad", out)
            self.assertEqual(d.text(), "clip")

    def test_falls_back_to_clipboard(self):
        with FakeDesktop(clipboard="copied paragraph", selection=None):
            out = dc.look_at_this()
            self.assertIn("clipboard", out)
            self.assertIn("copied paragraph", out)

    def test_nothing_anywhere_says_so(self):
        with FakeDesktop(clipboard=None, selection=None):
            self.assertIn("Nothing is selected", dc.look_at_this())

    def test_truncation(self):
        with FakeDesktop(clipboard="a" * 10000, selection=None):
            out = dc.look_at_this()
            self.assertIn("a" * dc.MAX_CHARS, out)
            self.assertNotIn("a" * (dc.MAX_CHARS + 1), out)
            self.assertIn("cut to the first", out)
        with FakeDesktop(clipboard="b" * 10000):
            self.assertEqual(len(dc.clipboard_text(max_chars=100)), 100)

    def test_clipboard_text_none_when_blank(self):
        with FakeDesktop(clipboard="   "):
            self.assertIsNone(dc.clipboard_text())

    def test_switch_off(self):
        with FakeDesktop(clipboard="x", selection="y") as d, \
                patch.dict(os.environ, {"DENISSE_DESKTOP_CONTEXT": "0"}):
            self.assertIn("switched off", dc.look_at_this())
            self.assertEqual(d.keys, [])


class Privacy(unittest.TestCase):
    def test_private_windows_are_not_read(self):
        for title, proc in [("1Password", "1Password.exe"), ("Vault - Bitwarden", "Bitwarden.exe"),
                            ("db.kdbx - KeePass", "KeePass.exe"), ("Sign in - Google Accounts", "chrome.exe"),
                            ("Log in to GitHub", "msedge.exe"), ("Barclays Bank - Online Banking", "chrome.exe"),
                            ("Untitled", "KeePassXC.exe")]:
            with FakeDesktop(clipboard="secret", selection="hunter2", title=title, process=proc) as d:
                out = dc.look_at_this()
                self.assertEqual(out, dc.BLOCKED, title)
                self.assertEqual(d.keys, [], title)   # not even a Ctrl+C
                self.assertNotIn(title, out)
                self.assertEqual(dc.context_line("what is this"), "", title)

    def test_credential_text_is_not_returned(self):
        for text in ["password: hunter2", "-----BEGIN RSA PRIVATE KEY-----\nabc", "API_KEY=sk-123"]:
            with FakeDesktop(clipboard=text, selection=None):
                self.assertEqual(dc.look_at_this(), dc.BLOCKED, text)

    def test_ordinary_text_mentioning_words_is_fine(self):
        with FakeDesktop(clipboard="The river bank flooded.", selection=None):
            self.assertIn("river bank", dc.look_at_this())


class CueRule(unittest.TestCase):
    def names(self, text):
        defs = [{"function": {"name": n}} for n in ("look_at_this", "get_weather", "calculate")]
        with patch.dict(os.environ, {"DENISSE_TOOL_ROUTER": "1"}):
            return {d["function"]["name"] for d in tool_router.select(defs, [{"role": "user", "content": text}])}

    def test_offered_only_with_a_deictic_cue(self):
        for text in ["explain this", "rewrite this shorter", "what does this error mean",
                     "translate that", "summarise the selected text", "what's on my clipboard",
                     "what's going on here"]:
            self.assertIn("look_at_this", self.names(text), text)
        for text in ["hey", "good morning", "what's the weather in London", "what is 17 times 23",
                     "thishouldnt match", "explain convolution"]:
            self.assertNotIn("look_at_this", self.names(text), text)

    def test_complaint_is_not_a_cue(self):
        self.assertNotIn("look_at_this", self.names("that's wrong"))

    def test_cue_does_not_break_follow_up_lookback(self):
        msgs = [{"role": "user", "content": "what's the weather in London?"},
                {"role": "assistant", "content": "..."},
                {"role": "user", "content": "and that?"}]
        self.assertEqual(tool_router.matched_groups(msgs), ["weather"])

    def test_context_line_only_for_vague_cued_requests(self):
        with FakeDesktop(title="main.py - Visual Studio Code", process="Code.exe"):
            line = dc.context_line("what does this error mean")
            self.assertIn("main.py - Visual Studio Code", line)
            self.assertIn("Code", line)
            self.assertEqual(dc.context_line("what's the weather in London"), "")
            self.assertEqual(dc.context_line(
                "can you explain to me in detail how the attention mechanism in this transformer paper works"), "")
        with FakeDesktop(title="Denisse", pid=os.getpid()):
            self.assertEqual(dc.context_line("what is this"), "")

    def test_context_line_is_short(self):
        with FakeDesktop(title="x" * 500):
            self.assertLess(len(dc.context_line("explain this")), 140)


class BrainWiring(unittest.TestCase):
    def test_tool_registered_and_window_line_never_saved(self):
        import brain
        self.assertIn("look_at_this", brain.tools.TOOL_FUNCTIONS)
        self.assertIn("look_at_this", {d["function"]["name"] for d in brain.TOOL_DEFINITIONS})
        seen = {}

        def chat(**kw):
            seen.setdefault("system", kw["messages"][0]["content"])
            return {"message": {"content": "It's a divide-by-zero."}}

        bot = brain.Brain.__new__(brain.Brain)
        with FakeDesktop(title="traceback.txt - Notepad"), \
                patch.object(brain.ollama, "chat", side_effect=chat), \
                patch.dict(brain.tools.TOOL_FUNCTIONS,
                           {n: (lambda **k: "Refused: isolated tool") for n in brain.tools.TOOL_FUNCTIONS},
                           clear=True):
            _reply, msgs = bot.respond([{"role": "user", "content": "what does this error mean"}])
        self.assertIn("traceback.txt - Notepad", seen["system"])
        self.assertFalse(any("traceback.txt" in str(m.get("content")) for m in msgs))


if __name__ == "__main__":
    unittest.main()
