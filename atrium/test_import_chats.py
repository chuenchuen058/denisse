"""Offline import tests. All inputs, vaults and CLI writes are temporary fixtures."""
import contextlib
import datetime as dt
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atrium import import_chats as importer


class ImportChatsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".review")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        # Construct synthetic keys at runtime so public-repo credential checks
        # don't mistake test data for a committed credential.
        self.key = "sk-" + "synthetic" * 5
        self.ask = 'Explain "signal": <sampling>?\napi_key=' + self.key + '\nUse a worked example.'
        self.answer = "Old answer."
        self.last = "Final explanation.\npassword: synthetic-password\nKeep the sample rate high."

    def write_jsonl(self, path, entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        return path

    def fixture(self, source):
        if source == "claude-code":
            return self.write_jsonl(self.inputs / "nested" / "session.jsonl", [
                {"timestamp": "2026-10-12T10:00:00Z", "sessionId": "same-id", "type": "user",
                 "message": {"role": "user", "content": self.ask}},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": self.answer},
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "hidden-tool-file"}}]}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "content": "hidden-tool-result"}]}},
                {"type": "assistant", "message": {"role": "assistant", "content": self.last}},
            ])
        if source == "codex":
            return self.write_jsonl(self.inputs / "nested" / "rollout.jsonl", [
                {"timestamp": "2026-10-12T10:00:00Z", "type": "session_meta",
                 "payload": {"id": "same-id", "timestamp": "2026-10-12T10:00:00Z"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": self.ask}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": self.ask}, {"type": "input_image", "image_url": "hidden-image"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                                        "content": "hidden-developer-message"}},
                {"type": "response_item", "payload": {"type": "function_call_output", "output": "hidden-tool-result"}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                        "content": self.answer}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": self.last}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "final",
                                                        "content": [{"type": "output_text", "text": self.last}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "analysis",
                                                        "content": "hidden-reasoning"}},
            ])
        path = self.inputs / "conversations.json"
        path.write_text(json.dumps([{
            "uuid": "same-id", "name": 'Signals: "sampling" / Windows? * title.',
            "created_at": "2026-10-12T10:00:00.000000Z", "chat_messages": [
                {"sender": "human", "text": self.ask},
                {"sender": "assistant", "text": self.answer},
                {"sender": "tool", "text": "hidden-tool-result"},
                {"sender": "assistant", "text": self.last},
            ]}]), encoding="utf-8")
        return path

    def run_import(self, source, path, apply=False):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            notes = importer.import_chats(source, path, self.vault, apply=apply)
        return notes, output.getvalue()

    def frontmatter(self, text):
        self.assertTrue(text.startswith("---\n"))
        block, body = text[4:].split("\n---\n", 1)
        return dict(line.split(": ", 1) for line in block.splitlines()), body

    def test_all_sources_dry_run_apply_and_idempotence(self):
        for source in importer.SOURCES:
            with self.subTest(source=source):
                path = self.fixture(source)
                before = {p.relative_to(self.root) for p in self.root.rglob("*")}
                planned, output = self.run_import(source, self.inputs)
                self.assertEqual(len(planned), 1)
                self.assertIn("Would write", output)
                self.assertEqual(before, {p.relative_to(self.root) for p in self.root.rglob("*")})
                notes, output = self.run_import(source, self.inputs, apply=True)
                self.assertEqual(planned, notes)
                note = notes[0]
                text = note.read_text(encoding="utf-8")
                fm, body = self.frontmatter(text)
                self.assertEqual(json.loads(fm["name"]), note.stem)
                self.assertEqual(json.loads(fm["description"]), 'Explain "signal": <sampling>? Use a worked example.')
                self.assertEqual(fm["updated"], dt.date.today().isoformat())
                self.assertEqual(fm["date"], "2026-10-12")
                self.assertEqual(fm["source"], source)
                self.assertEqual(fm["type"], "chat")
                self.assertEqual(fm["tags"], f"[chat, {source}]")
                self.assertIn("## What was asked\n\n- ", body)
                self.assertIn("## Outcome\n\nFinal explanation. Keep the sample rate high.", body)
                self.assertEqual(body.count("Use a worked example."), 1)
                self.assertNotIn(self.key, text)
                self.assertNotIn("synthetic-password", text)
                self.assertNotIn("Old answer.", text)
                self.assertNotIn("hidden-", text)
                self.assertTrue(note.is_relative_to(self.vault / "Calendar" / "Chats"))
                self.assertNotRegex(note.name, r'[<>:"/\\|?*]')
                self.assertIn(importer.markdown(str(path.resolve())) if source != "claude-export" else "same-id", body)
                self.assertEqual(self.run_import(source, path, apply=True)[0], [])
                self.assertEqual(self.run_import(source, path)[0], [])
                self.assertEqual(text, note.read_text(encoding="utf-8"))
        notes = list((self.vault / "Calendar" / "Chats").glob("*.md"))
        state = json.loads((self.vault / importer.STATE).read_text(encoding="utf-8"))
        self.assertEqual(len(notes), 3)
        self.assertEqual(set(state), {f"{s}:same-id" for s in importer.SOURCES})
        allowed = {self.vault / importer.STATE, *notes}
        self.assertEqual(allowed, {p for p in self.vault.rglob("*") if p.is_file()})

    def test_repeated_questions_legacy_events_and_malformed_lines(self):
        path = self.write_jsonl(self.inputs / "legacy.jsonl", [
            None, [], {"payload": "invalid payload"},
            {"timestamp": "2026-10-13T00:00:00Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "Try again"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Try again"}},
            {"type": "event_msg", "payload": {"type": "agent_reasoning", "text": "hidden-reasoning"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Resolved."}},
        ])
        with path.open("a", encoding="utf-8") as f:
            f.write('{"truncated":')
        notes, _ = self.run_import("codex", path, apply=True)
        text = notes[0].read_text(encoding="utf-8")
        self.assertEqual(text.count("- Try again\n"), 2)
        self.assertIn("Resolved.", text)
        self.assertNotIn("hidden-", text)

    def test_claude_markup_questions_blocks_and_bom(self):
        path = self.write_jsonl(self.inputs / "claude.jsonl", [
            None, [], {"message": "invalid message"},
            {"type": "user", "message": {"content": [
                {"type": "text", "text": "<svg> why is this invalid?"},
                {"type": "text", "text": "password: synthetic"},
                {"type": "text", "text": "Explain the viewBox."}]}},
            {"type": "user", "isMeta": True, "message": {"content": "hidden-meta"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Use a viewBox."},
                {"type": "text", "text": self.key},
                {"type": "text", "text": "Set its width."}]}},
        ])
        path.write_text("\ufeff" + path.read_text(encoding="utf-8"), encoding="utf-8")
        notes, _ = self.run_import("claude-code", path, apply=True)
        self.assertEqual(len(notes), 1)
        fm, body = self.frontmatter(notes[0].read_text(encoding="utf-8"))
        self.assertEqual(json.loads(fm["description"]), "<svg> why is this invalid? Explain the viewBox.")
        self.assertIn("Use a viewBox. Set its width.", body)
        self.assertNotIn(self.key, body)
        self.assertNotIn("synthetic", body)
        self.assertNotIn("hidden-meta", body)

    def test_skips_chats_without_safe_user_text(self):
        for source in importer.SOURCES:
            with self.subTest(source=source):
                self.ask = "password: synthetic\n" + self.key
                self.fixture(source)
                self.assertEqual(self.run_import(source, self.inputs, apply=True)[0], [])
                self.assertFalse(self.vault.exists())
                self.ask = ""
                self.fixture(source)
                self.assertEqual(self.run_import(source, self.inputs, apply=True)[0], [])

    def test_windows_titles_collision_and_summary_bounds(self):
        path = self.inputs / "conversations.json"
        records = [{"uuid": str(i), "name": 'CON:/\\?*"<>|' + "a" * 100 + "...  ",
                    "created_at": "2026-10-12", "chat_messages": [
                        {"sender": "human", "text": "x" * 1000},
                        {"sender": "human", "text": "Please inspect [[missing]] and [file](missing.md)."},
                        {"sender": "assistant", "text": "y" * 2000}]} for i in range(2)]
        path.write_text(json.dumps(records), encoding="utf-8")
        first, _ = self.run_import("claude-export", path)
        applied, _ = self.run_import("claude-export", path, apply=True)
        self.assertEqual(first, applied)
        self.assertEqual(len(set(applied)), 2)
        for note in applied:
            text = note.read_text(encoding="utf-8")
            fm, body = self.frontmatter(text)
            self.assertEqual(json.loads(fm["name"]), note.stem)
            self.assertLessEqual(len(note.stem.removeprefix("2026-10-12 claude-export ")), 60)
            self.assertFalse(note.stem.endswith((".", " ")))
            self.assertNotRegex(note.name, r'[<>:"/\\|?*]')
            asks = body.split("## What was asked\n\n")[1].split("\n\n## Outcome")[0].splitlines()
            self.assertLessEqual(len(asks[0][2:]), 300)
            self.assertLessEqual(len(body.split("## Outcome\n\n")[1].split("\n\n## Original")[0]), 800)
            self.assertNotIn("[[missing]]", text)
            self.assertNotIn("](missing.md)", text)
        self.assertEqual(self.run_import("claude-export", path, apply=True)[0], [])

    def test_credentials_removed_before_truncation_and_from_metadata(self):
        sensitive_lines = [self.key, "ghp_" + "a" * 31, "github_pat_" + "a" * 31,
                           "AIza" + "a" * 31, "xoxb-" + "a" * 25, "PASSWORD: fake",
                           "using passwd fake", "token = fake", "api-key: fake",
                           "-----BEGIN " + "RSA PRIVATE KEY-----", "private key contents",
                           "-----END " + "RSA PRIVATE KEY-----"]
        self.assertEqual(importer.clean("Safe\n" + "\n".join(sensitive_lines) + "\nUseful"), "Safe\nUseful")
        self.ask = "safe\n" + "x" * 400 + self.key
        self.last = "token: fake"
        path = self.fixture("claude-export")
        data = json.loads(path.read_text(encoding="utf-8"))
        data[0].update(name=self.key, uuid=self.key)
        path.write_text(json.dumps(data), encoding="utf-8")
        notes, output = self.run_import("claude-export", path, apply=True)
        contents = notes[0].read_text(encoding="utf-8") + (self.vault / importer.STATE).read_text(encoding="utf-8")
        self.assertNotIn(self.key, contents + output)
        self.assertNotIn("x" * 300, contents)
        self.assertIn("No assistant text available", contents)

    def test_existing_notes_not_overwritten_and_bad_state_refused(self):
        path = self.fixture("claude-export")
        planned, _ = self.run_import("claude-export", path)
        planned[0].parent.mkdir(parents=True)
        planned[0].write_text("Keep existing note", encoding="utf-8")
        notes, _ = self.run_import("claude-export", path, apply=True)
        self.assertNotEqual(notes, planned)
        self.assertEqual(planned[0].read_text(encoding="utf-8"), "Keep existing note")
        state_path = self.vault / importer.STATE
        state_path.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_import("claude-export", path, apply=True)
        self.assertEqual(state_path.read_text(encoding="utf-8"), "[]")

    def test_output_redirects_refused(self):
        path = self.fixture("claude-export")
        outside = self.root / "outside"
        outside.mkdir()
        self.vault.mkdir()
        # Real symlinks need a Windows privilege unavailable on some runners.
        # Mock resolution to exercise both directory and state-file redirects.
        resolve = Path.resolve
        for relative in (importer.CHATS, importer.STATE, importer.STATE.parent):
            def redirect(p, *args, **kwargs):
                return outside if p == self.vault / relative else resolve(p, *args, **kwargs)
            with self.subTest(relative=relative), mock.patch.object(Path, "resolve", redirect):
                with self.assertRaises(ValueError):
                    self.run_import("claude-export", path, apply=True)
            self.assertEqual(list(outside.iterdir()), [])

    def test_cli_defaults_dry_run_and_apply_with_fake_home(self):
        self.fixture("claude-code")
        with mock.patch.object(Path, "home", return_value=self.inputs), \
                mock.patch.object(importer, "import_chats", return_value=[]) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            for source, suffix in (("claude-code", ".claude/projects"), ("codex", ".codex/sessions")):
                self.assertEqual(importer.main(["--source", source, "--vault", str(self.vault)]), 0)
                self.assertEqual(run.call_args.args[1], self.inputs / suffix)
                self.assertFalse(run.call_args.kwargs["apply"])
        command = [sys.executable, "-m", "atrium.import_chats", "--source", "claude-code",
                   "--path", str(self.inputs), "--vault", str(self.vault)]
        dry = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("Would write", dry.stdout)
        self.assertFalse(self.vault.exists())
        applied = subprocess.run(command + ["--apply"], capture_output=True, text=True, timeout=30)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(len(list((self.vault / importer.CHATS).glob("*.md"))), 1)
        repeated = subprocess.run(command + ["--apply"], capture_output=True, text=True, timeout=30)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn("0 chat(s)", repeated.stdout)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(importer.main(["--source", "codex", "--path", str(self.root / "missing"),
                                            "--vault", str(self.vault)]), 1)

    def test_generated_notes_pass_vault_contract_in_fixture_only(self):
        self.ask = 'Explain [[missing]] and [example](missing.md): "why"?'
        for source in importer.SOURCES:
            self.run_import(source, self.fixture(source), apply=True)
        # The checker has a hardcoded real-vault path and no CLI override.
        # Redirect just that constant in memory; execute all its actual checks.
        command = """import ast, pathlib, sys
tree = ast.parse(pathlib.Path('scripts/vault_contract.py').read_text(encoding='utf-8'))
for statement in tree.body:
    if isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VAULT' for t in statement.targets):
        statement.value = ast.Constant(sys.argv[1])
exec(compile(ast.fix_missing_locations(tree), 'scripts/vault_contract.py', 'exec'))
"""
        (self.vault / "Denisse").mkdir()
        result = subprocess.run([sys.executable, "-c", command, str(self.vault)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("warnings : 0", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
