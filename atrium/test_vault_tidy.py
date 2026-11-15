"""scripts/tidy_vault_logs.py and the month-folder layout of the importer / chatlog / capture.

Every vault here is a temporary fixture; the real vault is never touched."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from atrium import chatlog, months
from atrium import import_chats as importer

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("tidy_vault_logs", REPO / "scripts" / "tidy_vault_logs.py")
tidy = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tidy  # dataclasses look their module up there
_spec.loader.exec_module(tidy)

PREAMBLE = r"# AGENTS.md instructions \<INSTRUCTIONS\> # Chuen — read the vault first This is a pointer…"
WT_PREAMBLE = (r"# AGENTS.md instructions for C:\\Users\\Xichu\\GitHub\\denisse-wt\\"
               r"2026-11-12T0104-orchestra-copilot-can-review \<INSTRUCTIONS\> # Chuen")


def chat_note(stem, asks, source="codex", date="2026-10-27", extra=""):
    lines = ["---", f"name: {json.dumps(stem, ensure_ascii=False)}", 'description: "x"', "updated: 2026-11-02",
             "type: chat", f"source: {source}", f"date: {date}", f"tags: [chat, {source}]", "---", "",
             f"# {importer.markdown(stem)}", "", "## What was asked", ""]
    lines += [f"- {a}" for a in asks]
    lines += ["", "## Outcome", "", "done", ""]
    return "\n".join(lines) + extra


class TidyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=REPO / ".review")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.v = (root / "vault").resolve()
        self.manifests = root / "manifests"
        self.lock = root / "vault.lock"
        self.chats = self.v / "Calendar" / "Chats"
        self.sessions = self.v / "Calendar" / "Sessions"

    def w(self, rel, text, mtime=None):
        p = self.v / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def snapshot(self):
        return {p.relative_to(self.v).as_posix(): p.read_bytes() for p in self.v.rglob("*") if p.is_file()}

    def dirs(self):
        return {p.relative_to(self.v).as_posix() for p in self.v.rglob("*") if p.is_dir()}

    def run_tidy(self, dry=False, root=False):
        out = io.StringIO()
        m = tidy.run(self.v, dry=dry, root=root, manifest_dir=self.manifests, lock_path=self.lock,
                     say=lambda s: out.write(s + "\n"))
        return m, out.getvalue()

    def fixture(self):
        self.w("Calendar/Chats/2026-10-12 claude-code fix the tray.md",
               chat_note("2026-10-12 claude-code fix the tray", ["fix the tray"], "claude-code", "2026-10-12",
                         extra="\nSee [the log](../Log/2026-10-12.md).\n"))
        old = "2026-10-27 codex # AGENTS.md instructions INSTRUCTIONS # Chuen — read the (1)"
        self.w(f"Calendar/Chats/{old}.md", chat_note(old, [PREAMBLE, r"\<external\_codex\_apps\_open\_page\>{}",
                                                           "could you add the solutions to electromagnetism week 5"]))
        old2 = "2026-10-27 codex # AGENTS.md instructions INSTRUCTIONS # Chuen — read the (2)"
        self.w(f"Calendar/Chats/{old2}.md", chat_note(old2, [PREAMBLE, "could you add the solutions to electromagnetism week 5"]))
        old3 = "2026-11-12 codex # AGENTS.md instructions for C Users Xichu GitHub denis (13)"
        self.w(f"Calendar/Chats/{old3}.md", chat_note(old3, [WT_PREAMBLE, "You are running one Orchestra task"],
                                                      date="2026-11-12"))
        # undated: frontmatter date, then mtime
        self.w("Calendar/Chats/hand written.md", "---\ntype: dated\ndate: 2026-09-03\n---\n# x\n")
        self.w("Calendar/Chats/loose.md", "no frontmatter\n", mtime=dt.datetime(2026, 8, 20, 12).timestamp())
        self.w("Calendar/Sessions/2026-11-02 denisse 12541099.md", "---\nname: Session-12541099\n---\n")
        self.w("Calendar/Log/2026-10-12.md",
               "# day\n\n## Chats\n\n- [[Calendar/Chats/2026-10-12 claude-code fix the tray|2026-10-12 claude-code fix the tray]]\n"
               "- bare [[2026-11-02 denisse 12541099]] stays\n"
               f"- renamed bare [[{old}]] follows\n"
               "- md [chat](Calendar/Chats/2026-10-12%20claude-code%20fix%20the%20tray.md)\n"
               "- web [x](https://example.com/Calendar/Chats/a.md)\n")
        self.w("Calendar/Handoff-2026-11-02.md", "- 10:00 [capture] session saved -> [[Calendar/Sessions/2026-11-02 denisse 12541099]]\n")
        self.w("Dev/chat-import-state.json", json.dumps({"codex:a": f"Calendar/Chats/{old}.md",
                                                         "claude-code:b": "Calendar/Chats/2026-10-12 claude-code fix the tray.md"}, indent=2) + "\n")
        self.w("Dev/capture-state.json", json.dumps({"12541099xx": {"bytes": 1, "started": "2026-11-02T10:00",
                                                                   "note": str(self.v / "Calendar/Sessions/2026-11-02 denisse 12541099.md")}}, indent=1))
        self.w(".obsidian/workspace.json", json.dumps({"lastOpenFiles": [f"Calendar/Chats/{old}.md"]}, ensure_ascii=False))
        return old, old2, old3

    def test_moves_renames_and_rewrites_links(self):
        old, old2, old3 = self.fixture()
        self.w("Calendar/Chats/2026-10/2026-10-12 claude-code fix the tray.md", "already there\n")  # a collision
        m, out = self.run_tidy()
        self.assertIsNotNone(m, out)
        self.assertEqual([p.name for p in self.chats.iterdir() if p.is_file()], [])  # base folder cleared
        self.assertEqual([p.name for p in self.sessions.iterdir() if p.is_file()], [])
        # dated, frontmatter-dated and mtime-dated
        self.assertTrue((self.sessions / "2026-11" / "2026-11-02 denisse 12541099.md").is_file())
        self.assertTrue((self.chats / "2026-09" / "hand written.md").is_file())
        self.assertTrue((self.chats / "2026-08" / "loose.md").is_file())
        # never overwritten: the file already there keeps its bytes, the moved one gets " (1)"
        oct_ = self.chats / "2026-10"
        self.assertEqual((oct_ / "2026-10-12 claude-code fix the tray.md").read_text(encoding="utf-8"), "already there\n")
        moved = oct_ / "2026-10-12 claude-code fix the tray (1).md"
        self.assertTrue(moved.is_file())
        # preamble names -> the first real ask, unique; the worktree one -> its task
        new1 = oct_ / "2026-10-27 codex could you add the solutions to electromagnetism week 5.md"
        new2 = oct_ / "2026-10-27 codex could you add the solutions to electromagnetism week 5 (1).md"
        self.assertTrue(new1.is_file() and new2.is_file(), sorted(p.name for p in oct_.iterdir()))
        self.assertTrue((self.chats / "2026-11" / "2026-11-12 codex orchestra-copilot-can-review.md").is_file())
        text = new1.read_text(encoding="utf-8")
        self.assertIn(f"name: {json.dumps(new1.stem, ensure_ascii=False)}\n", text)
        self.assertIn(f"\n# {importer.markdown(new1.stem)}\n", text)
        # the moved note's own relative link now climbs one more level
        self.assertIn("[the log](../../Log/2026-10-12.md)", moved.read_text(encoding="utf-8"))
        day = (self.v / "Calendar/Log/2026-10-12.md").read_text(encoding="utf-8")
        self.assertIn(f"[[Calendar/Chats/2026-10/{moved.stem}|2026-10-12 claude-code fix the tray]]", day)
        self.assertIn("bare [[2026-11-02 denisse 12541099]] stays", day)  # Obsidian resolves by name
        self.assertIn(f"renamed bare [[{new1.stem}]] follows", day)
        self.assertIn("(Calendar/Chats/2026-10/2026-10-12%20claude-code%20fix%20the%20tray%20%281%29.md)", day)
        self.assertIn("(https://example.com/Calendar/Chats/a.md)", day)
        self.assertIn("[[Calendar/Sessions/2026-11/2026-11-02 denisse 12541099]]",
                      (self.v / "Calendar/Handoff-2026-11-02.md").read_text(encoding="utf-8"))
        state = json.loads((self.v / "Dev/chat-import-state.json").read_text(encoding="utf-8"))
        self.assertIn(state["codex:a"], {f"Calendar/Chats/2026-10/{new1.name}", f"Calendar/Chats/2026-10/{new2.name}"})
        self.assertEqual(state["claude-code:b"], f"Calendar/Chats/2026-10/{moved.name}")
        cap = json.loads((self.v / "Dev/capture-state.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(cap["12541099xx"]["note"]), self.sessions / "2026-11" / "2026-11-02 denisse 12541099.md")
        # .obsidian is never written
        self.assertIn(old, (self.v / ".obsidian/workspace.json").read_text(encoding="utf-8"))
        manifest = json.loads(m.read_text(encoding="utf-8"))
        self.assertTrue(manifest["done"])
        self.assertEqual(len(manifest["moves"]), 7)
        self.assertFalse(self.lock.exists())  # the vault lock was released

    def test_dry_run_changes_nothing(self):
        self.fixture()
        before, dirs = self.snapshot(), self.dirs()
        m, out = self.run_tidy(dry=True, root=True)
        self.assertIsNone(m)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.dirs(), dirs)
        self.assertFalse(self.manifests.exists())
        self.assertIn("7 file(s) to move (3 renamed)", out)
        self.assertIn("Calendar/Chats/hand written.md -> Calendar/Chats/2026-09/hand written.md", out)

    def test_undo_restores_everything(self):
        self.fixture()
        self.w("STATE NOW.md", "---\nname: STATE NOW\n---\n")
        before, dirs = self.snapshot(), self.dirs()
        m, _ = self.run_tidy(root=True)
        self.assertNotEqual(self.snapshot(), before)
        out = tidy.undo(m)
        self.assertEqual(out["skipped"], [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.dirs(), dirs)  # the month folders it made are gone again

    def test_undo_leaves_a_note_edited_since(self):
        self.fixture()
        m, _ = self.run_tidy()
        day = self.v / "Calendar/Log/2026-10-12.md"
        day.write_text("Chuen edited this\n", encoding="utf-8")
        out = tidy.undo(m)
        self.assertEqual(day.read_text(encoding="utf-8"), "Chuen edited this\n")
        self.assertIn("Calendar/Log/2026-10-12.md", out["skipped"])

    def test_root_moves_state_now_keeps_pointers_and_never_overwrites(self):
        self.w("STATE NOW.md", "old root copy\n")
        self.w("CLAUDE.md", "---\nname: CLAUDE\n---\n# Claude: start here\n")
        self.w("GEMINI.md", "---\nname: GEMINI\n---\n")
        self.w("hot.md", "Live: [[STATE NOW]]\n")
        _, out = self.run_tidy(root=True)
        self.assertEqual((self.v / "Denisse/STATE NOW.md").read_text(encoding="utf-8"), "old root copy\n")
        self.assertFalse((self.v / "STATE NOW.md").exists())
        self.assertTrue((self.v / "CLAUDE.md").is_file() and (self.v / "GEMINI.md").is_file())
        self.assertIn("left at the root: CLAUDE.md", out)
        self.assertEqual((self.v / "hot.md").read_text(encoding="utf-8"), "Live: [[STATE NOW]]\n")
        # The runner already wrote the new copy: the old one is kept beside it, not over it.
        self.w("STATE NOW.md", "stale root copy\n")
        self.w("Denisse/STATE NOW.md", "live copy\n")
        self.run_tidy(root=True)
        self.assertEqual((self.v / "Denisse/STATE NOW.md").read_text(encoding="utf-8"), "live copy\n")
        self.assertEqual((self.v / "Denisse/STATE NOW (1).md").read_text(encoding="utf-8"), "stale root copy\n")
        self.assertEqual((self.v / "hot.md").read_text(encoding="utf-8"), "Live: [[STATE NOW]]\n")

    def test_without_root_flag_root_is_untouched(self):
        self.w("STATE NOW.md", "x\n")
        self.run_tidy()
        self.assertTrue((self.v / "STATE NOW.md").is_file())

    def test_second_run_has_nothing_to_do(self):
        self.fixture()
        self.run_tidy()
        before = self.snapshot()
        m, out = self.run_tidy()
        self.assertIsNone(m)
        self.assertIn("Nothing to tidy.", out)
        self.assertEqual(self.snapshot(), before)

    def test_importer_does_not_duplicate_after_the_move(self):
        src = Path(self.tmp.name) / "in" / "s.jsonl"
        src.parent.mkdir(parents=True)
        src.write_text("\n".join(json.dumps(e) for e in [
            {"timestamp": "2026-10-12T10:00:00Z", "sessionId": "sid-1", "type": "user",
             "message": {"role": "user", "content": "how do I route the tail cables"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "Like so."}}]) + "\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            [note] = importer.import_chats("claude-code", src, self.v, apply=True)
        self.assertEqual(note.parent, self.chats / "2026-10")  # new notes go straight into the month
        # Make it look like a pre-tidy vault (flat note, flat state path), then tidy.
        flat = self.chats / note.name
        os.rename(note, flat)
        state_file = self.v / importer.STATE
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state_file.write_text(json.dumps({k: f"Calendar/Chats/{flat.name}" for k in state}), encoding="utf-8")
        self.run_tidy()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.import_chats("claude-code", src, self.v, apply=True), [])
        self.assertEqual([p.name for p in months.notes(self.chats)], [note.name])
        self.assertEqual(set(json.loads(state_file.read_text(encoding="utf-8")).values()),
                         {f"Calendar/Chats/2026-10/{note.name}"})
        # A different chat with the same title gets its own name, even across month folders.
        src2 = src.with_name("t.jsonl")
        src2.write_text(src.read_text(encoding="utf-8").replace("sid-1", "sid-2"), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            [second] = importer.import_chats("claude-code", src2, self.v, apply=True)
        self.assertEqual(second.name, note.stem + " (1).md")
        # link_existing finds notes in month folders
        self.assertEqual(importer.link_existing(self.v, apply=False)["already"]
                         + importer.link_existing(self.v, apply=False)["empty"], 2)


class TitleAndPathsTest(unittest.TestCase):
    def test_ask_title_skips_preambles(self):
        self.assertEqual(importer.ask_title(["# AGENTS.md instructions <INSTRUCTIONS> blah", "<environment_context>x",
                                             "can i turn on the lights on my keyboard"]),
                         "can i turn on the lights on my keyboard")
        self.assertEqual(importer.ask_title(["<INSTRUCTIONS> # Chuen", "one two three four five six seven eight nine ten eleven"]),
                         "one two three four five six seven eight nine ten")
        self.assertEqual(importer.ask_title([WT_PREAMBLE.replace("\\\\", "\\"), "You are running one Orchestra task"]),
                         "orchestra-copilot-can-review")
        self.assertEqual(importer.ask_title(["# AGENTS.md instructions only"]), "Untitled chat")
        self.assertEqual(importer.ask_title(["a normal question?"]), "a normal question")

    def test_chatlog_uses_a_flat_note_left_from_before_the_tidy(self):
        with tempfile.TemporaryDirectory(dir=REPO / ".review") as t:
            v = Path(t)
            self.assertEqual(chatlog.note_path(v, "2026-11-15"), v / "Calendar/Chats/2026-11/2026-11-15 claude hub.md")
            flat = v / "Calendar/Chats/2026-11-15 claude hub.md"
            flat.parent.mkdir(parents=True)
            flat.write_text("x", encoding="utf-8")
            self.assertEqual(chatlog.note_path(v, "2026-11-15"), flat)

    def test_month_helpers(self):
        self.assertEqual(months.folder("2026-11-15"), "2026-11")
        self.assertEqual(months.name_date("2026-10-26 codex x.md"), "2026-10-26")
        self.assertIsNone(months.name_date("loose.md"))
        self.assertIsNone(months.name_date("2026-13-40 bad.md"))


if __name__ == "__main__":
    unittest.main()
