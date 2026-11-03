"""Offline fixture-tree coverage for the read-only hardcoding inventory."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hardcode_audit as audit


class HardcodeAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = audit.load_config()

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def scan(self, source, name="brain.py"):
        self.write(name, source)
        result = audit.audit(self.root, self.config)
        self.assertEqual(result.errors, [])
        return result

    def values(self, result, kind):
        return {finding.value for finding in result.findings if finding.kind == kind}

    def test_requested_categories_and_real_line_numbers(self):
        result = self.scan(
            'import re\n'
            'PATTERN = re.compile(r"^(start|stop)$")\n'
            'WORDS = ["please", "cancel"]\n'
            'def route(user_text):\n'
            '    return any(word in user_text for word in WORDS)\n'
            'VAULT = r"C:\\Users\\Xichu\\Denisse"\n'
            'MODEL = "llama3.1:8b"\n'
            'URL = "https://example.test/api"\n'
            'TIMEOUT = 12.5\n'
            'START = "08:45"\n'
            'limit = 7\n'
        )
        for kind in self.config["kinds"]:
            if kind != "numeric literal (review)":
                self.assertTrue(self.values(result, kind), kind)
        self.assertIn("'please'", self.values(result, "text match / keyword candidate"))
        self.assertEqual({f.line for f in result.findings if f.kind == "absolute path"}, {6})
        self.assertEqual({f.line for f in result.findings if f.value == "12.5"}, {9})

    def test_aliased_regex_calls_named_patterns_and_fstrings(self):
        result = self.scan(
            'import re as rx\n'
            'from re import search as seek\n'
            'PART = "start|stop"\n'
            'EXPR = rf"^({PART})$"\n'
            'compiled = rx.compile(EXPR)\n'
            'seek(pattern="hello", string=user_text)\n'
        )
        values = self.values(result, "regex candidate")
        self.assertIn("'start|stop'", values)
        self.assertIn("'hello'", values)
        self.assertIn("'^('", values)
        self.assertIn("')$'", values)

    def test_membership_comprehension_loop_aliases_and_string_methods(self):
        result = self.scan(
            'CUES = ("alpha", "beta")\n'
            'ALIASES = CUES\n'
            'def route(text):\n'
            '    for word in ALIASES:\n'
            '        if word in text:\n'
            '            return True\n'
            '    return text.startswith(("hello", "goodbye")) or text == "done"\n'
            'def second(text):\n'
            '    return any(w in text for w in CUES)\n'
        )
        self.assertTrue({"'alpha'", "'beta'", "'hello'", "'goodbye'", "'done'"}
                        <= self.values(result, "text match / keyword candidate"))

    def test_scope_separation_and_alias_cycles_terminate(self):
        result = self.scan(
            'import re\n'
            'def one(text):\n'
            '    pattern = "first"\n'
            '    return re.search(pattern, text)\n'
            'def two():\n'
            '    pattern = "second"\n'
            '    return pattern\n'
            'LEFT = RIGHT\n'
            'RIGHT = LEFT\n'
            're.search(LEFT, text)\n'
        )
        self.assertEqual(self.values(result, "regex candidate"), {"'first'"})

    def test_patterns_unpacked_from_mapping_and_sequence_iterators(self):
        result = self.scan(
            'import re\n'
            'GROUPS = {"files": ({"read_file"}, r"file|folder"),\n'
            '          "music": ({"play_song"}, r"music|song")}\n'
            'compiled = {name: re.compile(pat) for name, (tools, pat) in GROUPS.items()}\n'
            'MORE = [("greeting", "hello|hi")]\n'
            'for label, expr in MORE:\n'
            '    re.search(expr, text)\n'
        )
        self.assertEqual(self.values(result, "regex candidate"),
                         {"'file|folder'", "'music|song'", "'hello|hi'"})

    def test_paths_urls_names_models_in_strings_and_fstrings(self):
        result = self.scan(
            'a = "/opt/service/cache"\n'
            'b = r"\\\\server\\share\\notes"\n'
            'c = f"https://example.test/{topic}"\n'
            'd = "Hello, Chuen."\n'
            'e = "qwen2.5:7b"\n'
            'f = Path.home() / username\n'
        )
        self.assertEqual(len(self.values(result, "absolute path")), 2)
        self.assertIn("'https://example.test/'", self.values(result, "URL"))
        self.assertIn("'Hello, Chuen.'", self.values(result, "user / vault name"))
        self.assertIn("'qwen2.5:7b'", self.values(result, "model name"))

    def test_numeric_defaults_comparisons_slices_and_signed_values(self):
        result = self.scan(
            'import os\n'
            'NUM_CTX = int(os.environ.get("APP_CTX", "8192"))\n'
            'def work(timeout=15, *, max_items=9):\n'
            '    sleep(0.25)\n'
            '    options = {"temperature": 0.2, "keep_alive": -1}\n'
            '    return things[:30] if score > 0.8 else None\n'
        )
        specific = self.values(result, "time / duration / threshold / limit")
        self.assertTrue({"'8192'", "15", "9", "0.25", "0.2", "-1"} <= specific)
        self.assertTrue({"30", "0.8"} <= self.values(result, "numeric literal (review)"))
        self.assertFalse(self.values(result, "numeric literal (review)") & specific)

    def test_model_context_catches_names_without_known_family_prefixes(self):
        result = self.scan(
            'MODEL_NAME = os.environ.get("SPEECH_MODEL", "small.en")\n'
            'client.chat(model="bespoke-v2")\n'
        )
        self.assertTrue({"'small.en'", "'bespoke-v2'"} <= self.values(result, "model name"))

    def test_comments_docstrings_booleans_and_none_are_not_findings(self):
        result = self.scan(
            '"""Chuen uses https://example.test and /opt/vault for 30 seconds."""\n'
            '# TIMEOUT = 12\n'
            'def method():\n'
            '    """llama3.1:8b"""\n'
            '    return True, False, None\n'
        )
        self.assertEqual(result.findings, [])

    def test_tree_exclusions_and_eval_infrastructure(self):
        for name in [".venv/pkg.py", "denisse-wt/other/app.py", "tests/app.py",
                     "scripts/test_example.py", "pkg/helper_test.py", "conftest.py",
                     "eval/fixtures/example.py", "orchestra/local_team/fixtures/app.py",
                     "eval/cases.py", "eval/proposed_cases.py", "memory/private.py",
                     ".review/private.py", "vendor/pkg.py"]:
            self.write(name, 'raise RuntimeError("must not run")\nTIMEOUT = 99\n')
        self.write("app.py", "LIMIT = 5\n")
        self.write("eval/run_eval.py", "TIMEOUT = 6\n")
        self.write("scripts/helper.py", "TIMEOUT = 7\n")
        result = audit.audit(self.root, self.config)
        self.assertEqual(result.errors, [])
        self.assertEqual(set(result.files), {"app.py", "eval/run_eval.py", "scripts/helper.py"})
        self.assertNotIn("99", {f.value for f in result.findings})

    def test_never_executes_source(self):
        marker = self.root / "executed"
        result = self.scan(f'from pathlib import Path\nPath({str(marker)!r}).write_text("bad")\n')
        self.assertTrue(result.files)
        self.assertFalse(marker.exists())

    def test_junctions_and_symlinks_are_pruned(self):
        self.write("app.py", "LIMIT = 1\n")
        self.write("linked/package.py", "LIMIT = 2\n")
        self.write("alias.py", "LIMIT = 3\n")
        # Do not require Windows symlink privileges or create an external link.
        with patch.object(Path, "is_junction", lambda path: path.name == "linked"), \
                patch.object(Path, "is_symlink", lambda path: path.name == "alias.py"):
            paths = list(audit.discover(self.root, self.config["scan"]))
        self.assertEqual(paths, [self.root / "app.py"])

    def test_detection_and_scan_policy_can_be_changed_in_json(self):
        self.write("app.py", 'who = "Mira"\nother = "Chuen"\n')
        changed = copy.deepcopy(self.config)
        changed["rules"] = [{"selector": "literal", "kind": "user / vault name",
                             "type": "string", "value_pattern": "^Mira$"}]
        changed["kinds"]["user / vault name"]["home"] = "data file"
        path = self.write("policy.json", json.dumps(changed))
        result = audit.audit(self.root, audit.load_config(path))
        self.assertEqual([(f.value, f.home) for f in result.findings], [("'Mira'", "data file")])
        changed["scan"]["exclude_files"].append("app.py")
        self.assertFalse(audit.audit(self.root, changed).files)

    def test_invalid_python_is_reported_and_other_files_still_scanned(self):
        self.write("broken.py", "def invalid(:\n")
        self.write("good.py", "TIMEOUT = 4\n")
        result = audit.audit(self.root, self.config)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("broken.py", result.errors[0])
        self.assertTrue(result.findings)
        self.assertIn("INCOMPLETE", audit.render(result, self.config))

    def test_python_encoding_cookie_and_bom(self):
        self.write("bom.py", "\ufeffTIMEOUT = 8\n")
        (self.root / "legacy.py").write_bytes('# coding: latin-1\nURL = "https://ex.test/café"\n'.encode("latin-1"))
        result = audit.audit(self.root, self.config)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.files), 2)
        self.assertTrue(self.values(result, "URL"))

    def test_report_ranking_escaping_full_values_and_reproducibility(self):
        self.write("scripts/helper.py", "LIMIT = 12\n")
        self.write("brain.py", 'import re\nrule = re.compile("<tag>|`hello`\\nnext")\n')
        self.write("plain.py", "pass\n")
        result = audit.audit(self.root, self.config)
        rendered = audit.render(result, self.config)
        self.assertLess(rendered.index("## brain.py"), rendered.index("## scripts/helper.py"))
        self.assertLess(rendered.index("## plain.py"), rendered.index("## scripts/helper.py"))
        self.assertIn("&lt;tag&gt;&#124;`hello`\\nnext", rendered)
        self.assertIn("| - | No candidates | - | - |", rendered)
        self.assertEqual(rendered, audit.render(audit.audit(self.root, self.config), self.config))
        self.write("plain.py", "LIMIT = 3\n")
        self.assertNotEqual(result.fingerprint, audit.audit(self.root, self.config).fingerprint)

    def test_cli_success_and_error_statuses(self):
        self.write("app.py", "LIMIT = 7\n")
        args = ["--root", str(self.root), "--output", "report.md"]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(audit.main(args), 0)
            self.write("bad.py", "def bad(:\n")
            self.assertEqual(audit.main(args), 1)
            self.assertEqual(audit.main(["--root", str(self.root / "missing")]), 1)
        self.assertIn("INCOMPLETE", (self.root / "report.md").read_text(encoding="utf-8"))

    def test_invalid_config_fails_without_output(self):
        config = copy.deepcopy(self.config)
        config["rules"][0]["call_pattern"] = "["
        path = self.write("bad.json", json.dumps(config))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(audit.main(["--root", str(self.root), "--config", str(path)]), 1)
        self.assertFalse((self.root / self.config["report"]["output"]).exists())


if __name__ == "__main__":
    unittest.main()
