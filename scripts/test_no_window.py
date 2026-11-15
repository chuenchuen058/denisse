"""no_window.py: the one helper that keeps background jobs' child processes windowless.

    .venv\\Scripts\\python.exe -m unittest scripts.test_no_window -v
"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import no_window  # noqa: E402


class FakePopen:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


class NoWindowTest(unittest.TestCase):
    def test_flags_adds_create_no_window_on_windows(self):
        self.assertEqual(no_window.CREATE_NO_WINDOW, 0x08000000)
        self.assertTrue(no_window.flags(0, windows=True) & no_window.CREATE_NO_WINDOW)
        self.assertEqual(no_window.flags(0x200, windows=True), 0x200 | no_window.CREATE_NO_WINDOW)  # keeps the rest

    def test_a_caller_that_wants_its_own_console_keeps_it(self):
        for own in (0x10, 0x08):  # CREATE_NEW_CONSOLE, DETACHED_PROCESS
            self.assertEqual(no_window.flags(own, windows=True), own)

    def test_nothing_changes_off_windows(self):
        self.assertEqual(no_window.flags(0, windows=False), 0)
        cls = type("P", (FakePopen,), {})
        self.assertFalse(no_window.install(cls, windows=False))
        self.assertEqual(cls(["x"]).kwargs, {})

    def test_install_sets_the_flag_on_every_popen_and_is_idempotent(self):
        cls = type("P", (FakePopen,), {})
        self.assertTrue(no_window.install(cls, windows=True))
        wrapped = cls.__init__
        self.assertTrue(no_window.install(cls, windows=True))
        self.assertIs(cls.__init__, wrapped)  # not wrapped twice
        self.assertEqual(cls(["git", "status"]).kwargs["creationflags"], no_window.CREATE_NO_WINDOW)
        self.assertEqual(cls(["x"], creationflags=0x10).kwargs["creationflags"], 0x10)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_the_real_popen_after_install_runs_hidden(self):
        no_window.install()
        self.assertTrue(getattr(subprocess.Popen.__init__, no_window.MARK, False))
        r = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "ok")

    def test_background_entry_points_use_the_one_helper(self):
        """The runner, the hub's live page and digest, the night job and the eval install it;
        none keeps its own copy of the wrapper."""
        for rel in ("orchestra/runner.py", "atrium/live_task.py", "atrium/digest_task.py", "atrium/__main__.py",
                    "scripts/nightly.py", "eval/run_eval.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("no_window.install()", text, rel)
            self.assertNotIn("_quiet_popen_init", text, rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
