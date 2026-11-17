"""Run in Claude from the live Atrium page (atrium/run_claude.py): prompts, refusals, the launch."""
import json
import subprocess
import unittest
from unittest import mock

from atrium import run_claude


class RunClaudeTest(unittest.TestCase):
    def setUp(self):
        self.c = json.loads(json.dumps(run_claude.cfg()))  # a copy of the shipped section

    def test_shipped_section(self):
        self.assertIsNotNone(self.c, "live.board.run ships switched on")
        for kind in ("hot", "task"):
            self.assertIn(kind, self.c["prompts"])
            self.assertIn(self.c["required"][kind], self.c["fields"][kind])
        page = run_claude.page_cfg()
        self.assertEqual(page["token"], run_claude.TOKEN)
        self.assertNotIn("argv", page, "the page never sees how Claude is started")

    def test_hot_prompt_is_one_line_and_bounded(self):
        p = run_claude.build_prompt({"kind": "hot", "group": "Waiting on you", "text": "Skrine:\n wait  for Natalie"}, self.c)
        self.assertIn("Skrine: wait for Natalie", p)
        self.assertIn("Waiting on you", p)
        self.assertNotIn("\n", p)
        long = run_claude.build_prompt({"kind": "hot", "group": "g", "text": "x" * 10000}, self.c)
        self.assertLessEqual(len(long), self.c["max_chars"])

    def test_task_prompt_and_refusals(self):
        p = run_claude.build_prompt({"kind": "task", "id": "2026-11-16T1417-orchestra-land", "status": "Queued"}, self.c)
        self.assertIn("2026-11-16T1417-orchestra-land", p)
        bad = ({"kind": "nope", "text": "x"}, {"kind": "hot", "text": "  "}, {"kind": "task", "id": "../etc"},
               {"kind": "task", "id": "a b"}, {"kind": None})
        for body in bad:
            with self.subTest(body=body), self.assertRaises(ValueError):
                run_claude.build_prompt(body, self.c)

    def test_braces_in_text_do_not_break_the_template(self):
        p = run_claude.build_prompt({"kind": "hot", "group": "g", "text": "use {prompt} and {0}"}, self.c)
        self.assertIn("use {prompt} and {0}", p)

    def test_launch_needs_the_token_and_opens_claude_with_the_prompt(self):
        seen = []
        popen = lambda argv, **kw: seen.append((argv, kw))  # noqa: E731
        with mock.patch.object(run_claude, "cfg", return_value=self.c), \
                mock.patch.object(run_claude, "_claude", return_value="C:/claude.exe"):
            with self.assertRaises(PermissionError):
                run_claude.launch({"kind": "hot", "group": "g", "text": "hi", "token": "wrong"}, popen=popen)
            self.assertEqual(seen, [], "nothing starts without the token")
            out = run_claude.launch({"kind": "hot", "group": "g", "text": "hi", "token": run_claude.TOKEN}, popen=popen)
        self.assertTrue(out["ok"])
        argv, kw = seen[0]
        self.assertEqual(argv[0], "C:/claude.exe")
        self.assertEqual(argv[-1], out["prompt"])
        self.assertEqual(kw["creationflags"], getattr(subprocess, "CREATE_NEW_CONSOLE", 0))

    def test_off_means_no_run(self):
        off = dict(self.c, on=False)
        with mock.patch("atrium.conf.section", return_value={"board": {"run": off}}):
            self.assertIsNone(run_claude.cfg())
            self.assertIsNone(run_claude.page_cfg())
            with self.assertRaises(ValueError):
                run_claude.launch({"kind": "hot", "text": "x", "token": run_claude.TOKEN})


if __name__ == "__main__":
    unittest.main()
