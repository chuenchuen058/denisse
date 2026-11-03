"""GitHub summaries use temp files; no test modules or live services are run."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_checks


def row(suite, status="PASS", output="Ran 3 tests in 0.1s\n\nOK\n"):
    return {"suite": suite, "status": status,
            "passed": status in ("PASS", "SKIP-SLOW", "QUARANTINED"),
            "seconds": 1.2, "detail": "fixture", "output": output,
            "exit_code": 0 if status == "PASS" else 1, "refusals": []}


class GitHubSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "summary.md"
        env = patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def test_appends_table_failures_first_without_reordering_rows(self):
        self.path.write_text("Existing summary\n", encoding="utf-8")
        rows = [row("a_pass.py"), row("z_fail.py", "FAIL"), row("b_timeout.py", "TIMEOUT"),
                row("c_guard.py", "GUARD")]
        run_checks.github_summary(rows)
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("Existing summary\n"))
        table = [line for line in text.splitlines() if line.startswith("| <code>")]
        self.assertEqual(table, [
            "| <code>b_timeout.py</code> | TIMEOUT | 1.2 | 3 |",
            "| <code>c_guard.py</code> | GUARD | 1.2 | 3 |",
            "| <code>z_fail.py</code> | FAIL | 1.2 | 3 |",
            "| <code>a_pass.py</code> | PASS | 1.2 | 3 |",
        ])
        self.assertEqual(rows[0]["suite"], "a_pass.py")
        self.assertEqual(text.count("<details>"), 3)
        self.assertNotIn("<summary>a_pass.py", text)
        run_checks.github_summary([row("second_run.py")])
        appended = self.path.read_text(encoding="utf-8")
        self.assertTrue(appended.startswith(text))
        self.assertIn("second_run.py", appended)

    def test_failure_excerpt_is_first_twenty_lines_and_escaped(self):
        output = "\n".join(["</pre></details><script>& café"] + [f"line {n}" for n in range(2, 26)])
        run_checks.github_summary([row("bad|<module>.py", "FAIL", output)])
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("bad&#124;&lt;module&gt;.py", text)
        self.assertIn("&lt;/pre&gt;&lt;/details&gt;&lt;script&gt;&amp; café", text)
        self.assertIn("line 20</pre>", text)
        self.assertNotIn("line 21", text)
        self.assertNotIn("<script>", text)
        self.assertEqual(text.count("</details>"), 1)

    def test_skipped_unknown_and_empty_failure_output(self):
        rows = [row("skip.py", "SKIP-SLOW"), row("quarantine.py", "QUARANTINED"),
                row("unknown.py", output="finished"), row("crash.py", "FAIL", "")]
        for skipped in rows[:2]:
            del skipped["output"]
        run_checks.github_summary(rows)
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text.count("| 1.2 | - |"), 4)
        self.assertEqual(text.count("<details>"), 1)
        self.assertIn("(no output captured)", text)

    def test_unset_or_empty_environment_does_not_write(self):
        for value in (None, ""):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("GITHUB_STEP_SUMMARY", None)
                else:
                    os.environ["GITHUB_STEP_SUMMARY"] = value
                run_checks.github_summary([row("pass.py")])
                self.assertFalse(self.path.exists())

    def test_test_counts(self):
        cases = [("Ran 1 test in 0.1s\nOK", 1), ("Ran 0 tests in 0s\nOK", 0),
                 ("Ran 2 tests in 0s\nRan 4 tests in 0s\nFAILED", 4),
                 ("7/9 passed", 9), ("Checks: 3/5 OK", 5), ("Crash before tests", None)]
        for output, expected in cases:
            with self.subTest(output=output):
                self.assertEqual(run_checks.test_count(output), expected)

    def test_main_writes_summary_on_pass_and_failure_and_keeps_json_transcript_free(self):
        report = Path(self.tmp.name) / "checks.json"
        module = run_checks.ROOT / "scripts" / "test_fixture.py"
        for status, expected_exit in (("PASS", 0), ("FAIL", 1)):
            with self.subTest(status=status), \
                 patch.object(sys, "argv", ["run_checks.py", "--report", str(report)]), \
                 patch.object(sys, "platform", "win32"), \
                 patch.object(run_checks, "discover", return_value=[module]), \
                 patch.object(run_checks, "markers", return_value={}), \
                 patch.object(run_checks, "run_one", return_value=row("scripts/test_fixture.py", status)), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(run_checks.main(), expected_exit)
            text = self.path.read_text(encoding="utf-8")
            self.assertIn(f"| {status} | 1.2 | 3 |", text)
            result = json.loads(report.read_text(encoding="utf-8"))["results"][0]
            self.assertEqual(result["status"], status)
            self.assertNotIn("output", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
