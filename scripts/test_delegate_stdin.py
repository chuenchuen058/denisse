"""delegate: a prompt too long for a Windows command line goes on stdin, a short one stays an argument."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import delegate  # noqa: E402


class LongPrompt(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(delegate.shutil, "which", lambda exe: exe)
        p.start()
        self.addCleanup(p.stop)
        self.engine = next(n for n, s in delegate.CONFIG["engines"].items() if s.get("stdin_over_chars"))
        self.limit = delegate.CONFIG["engines"][self.engine]["stdin_over_chars"]

    def test_short_prompt_is_an_argument(self):
        cmd, stdin = delegate._command(self.engine, "short prompt")
        self.assertIn("short prompt", cmd)
        self.assertIsNone(stdin)

    def test_long_prompt_goes_on_stdin_and_the_rest_of_argv_stays(self):
        prompt = "x" * (self.limit + 1)
        cmd, stdin = delegate._command(self.engine, prompt)
        self.assertEqual(stdin, prompt.encode("utf-8"))
        self.assertNotIn(prompt, cmd)
        short, _ = delegate._command(self.engine, "p")
        self.assertEqual(len(cmd), len(short) - 1, "only the prompt argument was dropped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
