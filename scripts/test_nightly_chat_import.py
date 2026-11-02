"""Nightly chat-import step, against temporary homes, downloads and vaults only."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nightly_chat_import as step  # noqa: E402

NOW = 2_000_000_000.0          # a fixed "now"; mtimes are set relative to it
OLD = NOW - 2 * 3600           # settled: two hours ago
FRESH = NOW - 5 * 60           # five minutes ago: still running


class NightlyChatImportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.home, self.vault = root / "home", root / "vault"
        self.downloads = self.home / "Downloads"
        self.downloads.mkdir(parents=True)
        self.vault.mkdir()
        self.lines = []

    # -- fixtures -----------------------------------------------------------
    def session(self, n, mtime=OLD, source="claude-code"):
        if source == "claude-code":
            path = self.home / ".claude" / "projects" / "proj" / f"s{n}.jsonl"
            entries = [
                {"timestamp": "2026-10-12T10:00:00Z", "sessionId": f"cc-{n}", "type": "user",
                 "message": {"role": "user", "content": f"Question number {n}"}},
                {"type": "assistant", "message": {"role": "assistant", "content": f"Answer {n}"}}]
        else:
            path = self.home / ".codex" / "sessions" / f"r{n}.jsonl"
            entries = [
                {"type": "session_meta", "payload": {"id": f"cx-{n}", "timestamp": "2026-10-12T10:00:00Z"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                        "content": f"Codex question {n}"}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                        "content": f"Codex answer {n}"}}]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def export_data(self, *ids):
        return json.dumps([{"uuid": i, "name": f"Web chat {i}", "created_at": "2026-10-01T09:00:00Z",
                            "chat_messages": [{"sender": "human", "text": f"Web question {i}"},
                                              {"sender": "assistant", "text": f"Web answer {i}"}]}
                           for i in ids])

    def export_zip(self, name="claude-export-2026.zip", ids=("w1", "w2"), mtime=OLD):
        path = self.downloads / name
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("data/conversations.json", self.export_data(*ids))
            z.writestr("data/users.json", "[]")
        os.utime(path, (mtime, mtime))
        return path

    def run_step(self, **kw):
        kw.setdefault("commit", False)
        return step.run(self.vault, home=self.home, downloads=self.downloads, now=NOW,
                        say=self.lines.append, **kw)

    def notes(self):
        chats = self.vault / "Calendar" / "Chats"
        return sorted(p.name for p in chats.glob("*.md")) if chats.exists() else []

    def state(self):
        return json.loads((self.vault / "Dev" / "chat-import-state.json").read_text(encoding="utf-8"))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.vault, capture_output=True, text=True,
                              check=True).stdout.strip()

    # -- tests --------------------------------------------------------------
    def test_both_sources_imported_and_counts_logged(self):
        self.session(1)
        self.session(2, source="codex")
        counts = self.run_step()
        self.assertEqual(counts["imported"], 2)
        self.assertEqual(counts["failed"], 0)
        self.assertIn("claude-code:cc-1", self.state())
        self.assertIn("codex:cx-2", self.state())
        self.assertIn("  claude-code: 1 imported", self.lines)
        self.assertIn("  codex: 1 imported", self.lines)

    def test_skips_sessions_modified_in_last_30_minutes(self):
        self.session(1)
        fresh = self.session(2, mtime=FRESH)
        self.assertEqual(self.run_step()["imported"], 1)
        self.assertNotIn("claude-code:cc-2", self.state())
        # Once it settles, a later night picks it up.
        os.utime(fresh, (OLD, OLD))
        self.assertEqual(self.run_step()["imported"], 1)
        self.assertIn("claude-code:cc-2", self.state())

    def test_cap_limits_notes_per_night_and_backlog_drains(self):
        for n in range(5):
            self.session(n)
        self.session(9, source="codex")
        counts = self.run_step(cap=3)
        self.assertEqual(counts["imported"], 3)
        self.assertEqual(len(self.notes()), 3)
        self.assertIn("  codex: skipped, nightly cap of 3 reached", self.lines)
        self.assertEqual(self.run_step(cap=3)["imported"], 3)
        self.assertEqual(self.run_step(cap=3)["imported"], 0)
        self.assertEqual(len(self.notes()), 6)

    def test_export_zip_picked_up_once_and_download_left_alone(self):
        z = self.export_zip()
        before = (z.read_bytes(), z.stat().st_mtime)
        counts = self.run_step()
        self.assertEqual(counts["exports"], 1)
        self.assertEqual(counts["imported"], 2)
        self.assertIn("claude-export:w1", self.state())
        self.assertIn(step.EXPORT_KEY + step.sha256(z), self.state())
        self.assertEqual((z.read_bytes(), z.stat().st_mtime), before)
        self.assertEqual(sorted(p.name for p in self.downloads.iterdir()), [z.name])
        # Same file next night: hash recorded, nothing re-imported.
        again = self.run_step()
        self.assertEqual((again["exports"], again["imported"]), (0, 0))

    def test_plain_conversations_json_and_unrelated_downloads(self):
        (self.downloads / "conversations.json").write_text(self.export_data("p1"), encoding="utf-8")
        (self.downloads / "holiday.zip").write_bytes(b"not an export")
        os.utime(self.downloads / "conversations.json", (OLD, OLD))
        counts = self.run_step()
        self.assertEqual((counts["exports"], counts["imported"], counts["failed"]), (1, 1, 0))
        self.assertTrue((self.downloads / "conversations.json").exists())

    def test_export_still_downloading_is_skipped(self):
        self.export_zip(mtime=FRESH)
        self.assertEqual(self.run_step()["exports"], 0)
        self.assertFalse((self.vault / "Dev" / "chat-import-state.json").exists())

    def test_capped_export_hash_not_recorded_until_finished(self):
        z = self.export_zip(ids=("a", "b", "c"))
        self.run_step(cap=2)
        self.assertNotIn(step.EXPORT_KEY + step.sha256(z), self.state())
        counts = self.run_step(cap=2)
        self.assertEqual((counts["imported"], counts["exports"]), (1, 1))
        self.assertIn(step.EXPORT_KEY + step.sha256(z), self.state())

    def test_idempotent_across_nights(self):
        self.session(1)
        self.session(2, source="codex")
        self.export_zip()
        self.assertEqual(self.run_step()["imported"], 4)
        state, notes = self.state(), self.notes()
        counts = self.run_step()
        self.assertEqual(counts["imported"], 0)
        self.assertIn("  no new chats", self.lines)
        self.assertEqual((self.state(), self.notes()), (state, notes))

    def test_failure_in_one_source_is_logged_and_others_continue(self):
        from unittest import mock
        from atrium import import_chats as importer
        real = importer.import_chats

        def flaky(source, *a, **kw):
            if source == "claude-code":
                raise OSError("private text that must not reach the log")
            return real(source, *a, **kw)
        self.session(1)
        self.session(2, source="codex")
        with mock.patch.object(importer, "import_chats", flaky):
            counts = self.run_step()
        self.assertEqual((counts["imported"], counts["failed"]), (1, 1))
        self.assertIn("codex:cx-2", self.state())
        self.assertFalse(any("private text" in l for l in self.lines))

    def test_bad_state_is_logged_not_raised(self):
        self.session(1, source="codex")
        bad = self.vault / "Dev" / "chat-import-state.json"
        bad.parent.mkdir()
        bad.write_text("[]", encoding="utf-8")      # importer refuses this state
        counts = self.run_step()
        self.assertEqual(counts["imported"], 0)
        self.assertGreaterEqual(counts["failed"], 1)
        self.assertTrue(any("import failed" in l for l in self.lines))

    def test_missing_directories_are_not_failures(self):
        counts = step.run(self.vault, home=self.home / "nobody", now=NOW,
                          commit=False, say=self.lines.append)
        self.assertEqual((counts["imported"], counts["failed"]), (0, 0))

    def test_dry_run_writes_nothing(self):
        self.session(1)
        counts = self.run_step(apply=False, commit=True)
        self.assertEqual((counts["imported"], counts["commit"]), (1, "none"))
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_commit_stages_only_new_notes_and_state(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.test")
        (self.vault / "other.md").write_text("initial", encoding="utf-8")
        old_note = self.vault / "Calendar" / "Chats" / "hand written.md"
        old_note.parent.mkdir(parents=True)
        old_note.write_text("mine", encoding="utf-8")
        self.git("add", "other.md")
        self.git("commit", "-qm", "initial")
        # Another session's staged work and an untracked stray must survive.
        (self.vault / "other.md").write_text("parallel session", encoding="utf-8")
        self.git("add", "other.md")
        (self.vault / "stray.md").write_text("untracked", encoding="utf-8")
        self.session(1)
        self.export_zip(ids=("w1",))

        counts = self.run_step(commit=True)
        self.assertEqual(counts["imported"], 2)
        self.assertNotIn(counts["commit"], ("none", "failed"))
        self.assertEqual(self.git("log", "-1", "--format=%s"), step.MESSAGE)
        committed = set(self.git("show", "--format=", "--name-only", "HEAD").splitlines())
        expected = {f"Calendar/Chats/{n}" for n in self.notes() if n != "hand written.md"}
        expected.add("Dev/chat-import-state.json")
        self.assertEqual(committed, expected)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "other.md")
        status = self.git("status", "--porcelain")
        self.assertIn("?? stray.md", status)
        self.assertIn("?? Calendar/Chats/hand written.md", status.replace('"', ""))

        # Nothing new the next night: no empty commit.
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.run_step(commit=True)["commit"], "none")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_commit_failure_is_reported_not_raised(self):
        self.session(1)   # vault is not a git repo, so the commit fails
        counts = self.run_step(commit=True)
        self.assertEqual((counts["imported"], counts["commit"]), (1, "failed"))
        self.assertTrue(any("commit failed" in l for l in self.lines))


if __name__ == "__main__":
    unittest.main()
