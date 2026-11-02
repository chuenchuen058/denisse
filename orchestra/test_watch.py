"""Offline tests for the live lane-log view, using only temporary review files.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_watch -v
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import watch  # noqa: E402


def assistant(*content):
    return json.dumps({
        "type": "assistant", "session_id": "test-session", "parent_tool_use_id": None,
        "message": {"id": "msg_test", "role": "assistant", "content": list(content)},
    }, ensure_ascii=False)


class DescribeTest(unittest.TestCase):
    def test_system_init(self):
        line = json.dumps({"type": "system", "subtype": "init", "model": "claude-sonnet-4-5",
                           "session_id": "test-session", "tools": ["Bash", "Read"]})
        self.assertEqual(watch.describe(line), "▶ session started (claude-sonnet-4-5)")

    def test_assistant_text(self):
        line = assistant({"type": "text", "text": "  Checking the tests.\n  Then the fix.  "})
        self.assertEqual(watch.describe(line), "💬 Checking the tests. Then the fix.")

    def test_assistant_tool_arguments(self):
        for name, args, expected in (
            ("Bash", {"command": "python -m unittest", "description": "Run tests"}, "python -m unittest"),
            ("Read", {"file_path": "orchestra/watch.py", "offset": 1}, "orchestra/watch.py"),
            ("Grep", {"pattern": "def describe", "path": "orchestra"}, "def describe"),
            ("Glob", {"path": "orchestra"}, "orchestra"),
        ):
            with self.subTest(name=name):
                line = assistant({"type": "tool_use", "id": "tool_test", "name": name, "input": args})
                self.assertEqual(watch.describe(line), f"🔧 {name} {expected}")

    def test_multiple_assistant_blocks(self):
        line = assistant({"type": "text", "text": "Working."},
                         {"type": "tool_use", "id": "tool_test", "name": "Bash",
                          "input": {"command": "git status"}},
                         {"type": "text", "text": "  "})
        self.assertEqual(watch.describe(line), "💬 Working.\n🔧 Bash git status")

    def test_result_with_cost(self):
        line = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                           "num_turns": 3, "total_cost_usd": 0.1234, "result": "Done."})
        self.assertEqual(watch.describe(line), "✅ finished (success), 3 turns, $0.12")

    def test_result_without_cost(self):
        self.assertEqual(watch.describe('{"type":"result","subtype":"success","num_turns":1}'),
                         "✅ finished (success), 1 turns")

    def test_garbage_and_plain_text(self):
        for line, expected in (("  {broken json\n", "{broken json"),
                               ("  Running\tchecks now.\n", "Running checks now."),
                               ("[not-json]", "[not-json]")):
            with self.subTest(line=line):
                self.assertEqual(watch.describe(line), expected)

    def test_noise_and_uninteresting_events_are_dropped(self):
        for line in ("", " \n", "    + CategoryInfo : SecurityError: (:) [], PSSecurityException",
                     "    + FullyQualifiedErrorId : UnauthorizedAccess", assistant(),
                     '{"type":"system","subtype":"status","status":"compacting"}',
                     '{"type":"user","message":{"content":[]}}'):
            with self.subTest(line=line):
                self.assertIsNone(watch.describe(line))

    def test_long_text_and_commands_are_shortened(self):
        self.assertEqual(watch.describe("x" * 200), "x" * 159 + "…")
        line = assistant({"type": "tool_use", "name": "Bash", "input": {"command": "x" * 200}})
        self.assertEqual(watch.describe(line), "🔧 Bash " + "x" * 119 + "…")


class ReviewTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="watch-test-")
        self.addCleanup(temp.cleanup)
        self.review = Path(temp.name) / ".review"
        (self.review / "reports").mkdir(parents=True)
        review_patch = patch.object(watch, "REVIEW", self.review)
        review_patch.start()
        self.addCleanup(review_patch.stop)

    def lock(self, slot, **data):
        path = self.review / f"runner.{slot}.lock"
        path.write_text(json.dumps({"pid": 1234, "since": "2026-11-02T22:54:00", **data}),
                        encoding="utf-8")
        return path

    def log(self, slug, *lines, append=False):
        path = self.review / "reports" / f"{slug}.lane.log"
        with path.open("a" if append else "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return path

    def poll(self, state):
        out = io.StringIO()
        with redirect_stdout(out):
            watch.poll_once(state)
        return out.getvalue()


class RunningTest(ReviewTest):
    def test_primary_and_numbered_slots_with_and_without_tasks(self):
        for idle_slot, busy_slot in (("claude", "claude.2"), ("claude.2", "claude")):
            with self.subTest(idle_slot=idle_slot):
                self.lock(idle_slot)
                self.lock(busy_slot, task="task-two")
                self.assertEqual(watch.running(), {busy_slot: "task-two"})
        self.lock("claude.2", task="task-one")
        self.assertEqual(watch.running(), {"claude": "task-two", "claude.2": "task-one"})

    def test_corrupt_lock_and_empty_tasks_are_ignored(self):
        self.lock("claude", task="busy")
        self.lock("claude.2", task=None)
        self.lock("codex", task="")
        (self.review / "runner.broken.lock").write_text("{partial", encoding="utf-8")
        (self.review / "state.lock").write_text('{"task":"not-a-runner"}', encoding="utf-8")
        self.assertEqual(watch.running(), {"claude": "busy"})

    def test_empty_or_missing_review(self):
        self.assertEqual(watch.running(), {})
        with patch.object(watch, "REVIEW", self.review / "absent"):
            self.assertEqual(watch.running(), {})


class PollTest(ReviewTest):
    slug = "2026-11-02T2254-watch-task"

    def test_follow_prints_only_appended_events(self):
        self.lock("claude", task=self.slug)
        log = self.log(self.slug, assistant({"type": "text", "text": "First café ☕"}))
        state = watch.WatchState()
        first = self.poll(state)
        self.assertIn("💬 First café ☕", first)
        self.assertIn(f"claude: {self.slug}", first)
        self.assertEqual(state.offsets[self.slug], log.stat().st_size)
        self.assertEqual(self.poll(state), "")

        self.log(self.slug, assistant({"type": "text", "text": "Second event"},
                                      {"type": "tool_use", "name": "Read",
                                       "input": {"file_path": "orchestra/watch.py"}}),
                 "plain Codex output", " + CategoryInfo : noise", append=True)
        second = self.poll(state)
        prefix = f"{watch.COLOURS[0]}[{self.slug[17:45]}]{watch.RESET} "
        self.assertEqual(second, prefix + "💬 Second event\n" +
                         prefix + "🔧 Read orchestra/watch.py\n" + prefix + "plain Codex output\n")
        self.assertEqual(state.offsets[self.slug], log.stat().st_size)
        self.assertEqual(self.poll(state), "")

    def test_initial_tail_counts_events_after_filtering(self):
        self.lock("claude", task=self.slug)
        self.log(self.slug, "old event", "recent event", " + CategoryInfo : noise",
                 assistant({"type": "text", "text": "last event"}), '{"type":"user"}')
        state = watch.WatchState(tail=2)
        output = self.poll(state)
        self.assertNotIn("old event", output)
        self.assertIn("recent event", output)
        self.assertIn("💬 last event", output)
        self.assertNotIn("CategoryInfo", output)
        self.assertEqual(self.poll(state), "")

    def test_shorter_replacement_restarts_from_zero_without_tail_limit(self):
        self.lock("claude", task=self.slug)
        log = self.log(self.slug, "original event " * 30)
        state = watch.WatchState(tail=1)
        self.poll(state)
        replacement = log.with_suffix(".replacement")
        replacement.write_text("new first\nnew second\n", encoding="utf-8")
        self.assertLess(replacement.stat().st_size, state.offsets[self.slug])
        replacement.replace(log)
        output = self.poll(state)
        self.assertIn("new first", output)
        self.assertIn("new second", output)
        self.assertNotIn("original event", output)
        self.assertEqual(state.offsets[self.slug], log.stat().st_size)
        self.assertEqual(self.poll(state), "")

    def test_empty_truncation_then_append(self):
        self.lock("claude", task=self.slug)
        self.log(self.slug, "original event " * 30)
        state = watch.WatchState()
        self.poll(state)
        self.log(self.slug)
        self.assertEqual(self.poll(state), "")
        self.assertEqual(state.offsets[self.slug], 0)
        self.log(self.slug, "after truncation", append=True)
        self.assertIn("after truncation", self.poll(state))
        self.assertEqual(self.poll(state), "")

    def test_missing_log_can_appear_later(self):
        self.lock("claude", task=self.slug)
        state = watch.WatchState()
        self.assertIn(self.slug, self.poll(state))
        self.assertNotIn(self.slug, state.offsets)
        self.log(self.slug, "now available")
        self.assertIn("now available", self.poll(state))

    def test_multiple_lanes_and_banner_changes(self):
        state = watch.WatchState()
        self.assertIn("nothing running", self.poll(state))
        self.assertEqual(self.poll(state), "")
        lock = self.lock("claude", task=self.slug)
        other = "2026-11-02T2254-other-task"
        self.lock("claude.2", task=other)
        self.log(self.slug, "first lane")
        self.log(other, "second lane")
        output = self.poll(state)
        # Lock filenames sort lexically: runner.claude.2.lock precedes runner.claude.lock.
        self.assertIn(f"{watch.COLOURS[0]}[{other[17:45]}]", output)
        self.assertIn(f"{watch.COLOURS[1]}[{self.slug[17:45]}]", output)
        self.log(other, "second lane append", append=True)
        self.assertIn("second lane append", self.poll(state))
        self.assertEqual(self.poll(state), "")
        lock.unlink()
        self.assertIn(f"running → claude.2: {other}", self.poll(state))
        self.assertEqual(self.poll(state), "")

    def test_cli_once_honours_tail_and_does_not_sleep(self):
        self.lock("claude", task=self.slug)
        self.log(self.slug, "hidden event", "visible event")
        out = io.StringIO()
        with patch.object(sys, "argv", ["watch.py", "--once", "--tail", "1"]), \
                patch.object(watch.os, "system"), patch.object(watch.time, "sleep") as sleep, \
                redirect_stdout(out):
            self.assertEqual(watch.main(), 0)
        sleep.assert_not_called()
        self.assertNotIn("hidden event", out.getvalue())
        self.assertIn("visible event", out.getvalue())

    def test_cli_follow_keeps_state_between_two_second_polls(self):
        self.lock("claude", task=self.slug)
        self.log(self.slug, "before sleep")
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 1:
                self.log(self.slug, "after sleep", append=True)
            else:
                raise KeyboardInterrupt

        out = io.StringIO()
        with patch.object(sys, "argv", ["watch.py"]), patch.object(watch.os, "system"), \
                patch.object(watch.time, "sleep", side_effect=sleep), redirect_stdout(out):
            with self.assertRaises(KeyboardInterrupt):
                watch.main()
        self.assertEqual(polls, [2, 2])
        self.assertEqual(out.getvalue().count("before sleep"), 1)
        self.assertEqual(out.getvalue().count("after sleep"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
