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


class LinkChatsTest(unittest.TestCase):
    """Related lines, day-note Chats sections and the --link-existing backfill,
    all against a fixture vault in a temp dir."""
    DAY = "2026-10-12"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".review")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.inputs = self.root / "inputs"
        self.write(f"Calendar/Log/{self.DAY}.md", f'---\nname: "{self.DAY}"\nprotection: append\n---\n\n'
                   "# 12 October\n\n## Work\n\n- did things\n")
        self.write(f"Calendar/Handoff-{self.DAY}.md", "---\nprotection: append\n---\n\n## Log\n")
        self.write("Atlas/Projects/Robotic Tail/Robotic Tail.md", "---\nname: Robotic Tail\n---\n# Robotic Tail\n")
        em = "Atlas/Academic/SEM 1 Modules/5CCYB010 Electromagnetism"
        self.write(f"{em}/Electromagnetism notes.md", "---\naliases: [5CCYB010]\n---\n# hub\n")
        self.write(f"{em}/Electromagnetism concepts.md", "# concepts\n")
        self.write(f"{em}/Coursework.md", "# cw\n")

    def write(self, rel, text):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        return p

    def chat(self, ask, name="session", day=DAY):
        path = self.inputs / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in [
            {"timestamp": f"{day}T10:00:00Z", "sessionId": name, "type": "user",
             "message": {"role": "user", "content": ask}},
            {"type": "assistant", "message": {"role": "assistant", "content": "Done."}}]) + "\n",
            encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            return importer.import_chats("claude-code", path, self.vault, apply=True)[0]

    def related(self, note):
        lines = [l for l in note.read_text(encoding="utf-8").splitlines() if l.startswith("Related:")]
        self.assertLessEqual(len(lines), 1)
        return lines[0] if lines else None

    def day_text(self):
        return (self.vault / f"Calendar/Log/{self.DAY}.md").read_text(encoding="utf-8")

    def snapshot(self):
        return {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}

    def backfill(self, apply):
        with contextlib.redirect_stdout(io.StringIO()):
            return importer.link_existing(self.vault, apply=apply)

    def test_new_chat_links_day_handoff_and_matching_project(self):
        note = self.chat("How should the robotic tail cables be routed?")
        line = self.related(note)
        self.assertEqual(line, "Related: " + " · ".join([
            f"[[Calendar/Log/{self.DAY}|{self.DAY}]]", f"[[Calendar/Handoff-{self.DAY}|Handoff-{self.DAY}]]",
            "[[Atlas/Projects/Robotic Tail/Robotic Tail|Robotic Tail]]"]))
        self.assertTrue(note.read_text(encoding="utf-8").endswith("\n\n" + line + "\n"))
        self.assertNotIn("Electromagnetism", line)
        # ...and the day hangs the chat off itself.
        self.assertIn(f"## Chats\n\n- [[Calendar/Chats/{note.stem}|{note.stem}]]\n", self.day_text())

    def test_module_alias_and_hub_name_match(self):
        line = self.related(self.chat("5CCYB010 question: explain electromagnetism concepts"))
        self.assertIn("|Electromagnetism concepts]]", line)
        self.assertIn("|Electromagnetism notes]]", line)
        self.assertNotIn("Coursework", line)  # excluded/ambiguous names link nothing

    def test_no_link_when_nothing_matches_or_exists(self):
        note = self.chat("What is a good pasta recipe?", day="2026-10-13")
        self.assertIsNone(self.related(note))
        self.assertNotIn("pasta", self.day_text())

    def test_max_related_links_cap(self):
        config = json.loads(Path(importer.conf.DEFAULT).read_text(encoding="utf-8-sig"))
        config["import_chats"]["max_related_links"] = 2
        alt = self.root / "config.json"
        alt.write_text(json.dumps(config), encoding="utf-8")
        with mock.patch.dict("os.environ", {"ATRIUM_CONFIG": str(alt)}):
            note = self.chat("robotic tail and electromagnetism, 5CCYB010")
        self.assertEqual(self.related(note).count("[["), 2)

    def test_day_chats_section_added_once_and_new_chats_join_it(self):
        first = self.chat("robotic tail", name="a")
        text = self.day_text()
        self.assertEqual(text.count("## Chats"), 1)
        self.write(f"Calendar/Log/{self.DAY}.md", text + "\n## Later entry\n\nwritten after\n")
        before = self.day_text()
        second = self.chat("something else", name="b")
        after = self.day_text()
        added = f"- [[Calendar/Chats/{second.stem}|{second.stem}]]\n"
        self.assertEqual(after.count("## Chats"), 1)
        self.assertIn(f"|{first.stem}]]\n" + added, after)  # inside the section, not at the end
        self.assertEqual(after.replace(added, ""), before)
        self.assertEqual(self.backfill(True)["lines"], 0)
        self.assertEqual(self.day_text(), after)

    def test_unprotected_day_note_is_locked_and_never_written(self):
        self.write(f"Calendar/Log/{self.DAY}.md", "---\nname: x\n---\n\n# day\n")
        before = self.day_text()
        note = self.chat("robotic tail")
        self.assertEqual(self.day_text(), before)
        self.assertIn(f"[[Calendar/Log/{self.DAY}|", self.related(note))  # linking TO it is fine
        self.assertEqual(self.backfill(False)["locked_days"], 1)

    def test_backfill_dry_run_writes_nothing_then_apply_then_idempotent(self):
        # An old-style chat note: no Related line, no protection field, CRLF.
        old = self.vault / f"Calendar/Chats/{self.DAY} codex tail.md"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes((f'---\r\nname: "{old.stem}"\r\ntype: chat\r\nsource: codex\r\ndate: {self.DAY}\r\n'
                         "---\r\n\r\n# tail\r\n\r\n## What was asked\r\n\r\n- fix the robotic tail\r\n").encode())
        self.write(f"Calendar/Chats/{self.DAY} codex locked.md",
                   f"---\ntype: chat\ndate: {self.DAY}\nprotection: locked\n---\n\n# x\n")
        self.write("Calendar/Chats/not a chat.md", "---\ntype: dated\n---\n# x\n")
        original = old.read_bytes()
        before = self.snapshot()
        out = self.backfill(False)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((out["chats"], out["locked_chats"], out["lines"]), (1, 1, 2))
        self.assertIn("Robotic Tail", out["sample"][0])
        out = self.backfill(True)
        self.assertEqual((out["chats"], out["lines"]), (1, 2))
        data = old.read_bytes()
        self.assertTrue(data.startswith(original))
        self.assertTrue(data.endswith(b"|Robotic Tail]]\r\n"))
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertNotIn("not a chat", self.day_text())
        settled = self.snapshot()
        out = self.backfill(True)
        self.assertEqual(self.snapshot(), settled)
        self.assertEqual((out["chats"], out["lines"], out["already"]), (0, 0, 1))

    def test_cli_backfill_dry_run(self):
        from atrium import __main__ as atrium_main
        self.write(f"Calendar/Chats/{self.DAY} codex tail.md",
                   f"---\ntype: chat\ndate: {self.DAY}\n---\n\n# tail\n\n## What was asked\n\n- robotic tail\n")
        before = self.snapshot()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = atrium_main.main(["import-chats", "--link-existing", "--dry-run", "--vault", str(self.vault)])
        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("dry run", output.getvalue())
        self.assertIn("1 chat note(s) would get", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
