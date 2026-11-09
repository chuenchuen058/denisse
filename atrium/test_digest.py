"""python -m unittest atrium.test_digest -v

The laptop digest (atrium/digest.py) and the shared chat-note writer (atrium/chatlog.py)
against a temp vault (a git repo), a temp Denisse repo and a fake runner.log. Nothing
here touches the real vault, the real repo's .review, or the scheduler."""
import copy
import datetime as dt
import importlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import testing_git
from atrium import conf

SHIPPED = json.loads(conf.DEFAULT.read_text(encoding="utf-8"))
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_NOSYSTEM": "1"}


def w(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}).stdout


def commit(cwd, rel, text, msg, when):
    w(cwd, rel, text)
    stamp = when.astimezone().isoformat(timespec="seconds")
    git(cwd, "add", "--", rel)
    git(cwd, "commit", "-q", "-m", msg, env={"GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp})


def stamp(t):
    return t.isoformat(timespec="seconds")


class DigestTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)  # first: no inherited GIT_* reaches a real repo
        self.tmp = Path(tempfile.mkdtemp())
        self.v, self.r = self.tmp / "vault", self.tmp / "repo"
        self.cfg_path = self.tmp / "atrium.json"
        self.cfg = copy.deepcopy(SHIPPED)
        self.env = mock.patch.dict(os.environ, {**GIT_ENV, "DENISSE_VAULT": str(self.v),
                                                "ATRIUM_CONFIG": str(self.cfg_path)})
        self.env.start()
        for d in (self.v, self.r):
            d.mkdir()
            git(d, "init", "-q")
        self.now = dt.datetime.now().replace(microsecond=0)
        self.recent, self.old = self.now - dt.timedelta(minutes=30), self.now - dt.timedelta(hours=5)
        self.write_cfg()
        commit(self.v, "hot.md", "# Hot\n", "vault: an old commit", self.old)
        commit(self.v, "Atlas/Academic/SEM 1 Modules/Maths/Maths Week 3.md", "notes\n",
               "Don: Maths week 3 tutor note", self.recent)
        commit(self.r, "a.py", "x = 1\n", "an old repo commit", self.old)
        commit(self.r, "b.py", "y = 2\n", "tray: fix the mic toggle", self.recent)
        w(self.r, ".review/runner.log", "\n".join([
            f"{stamp(self.old)} PASS old-task after 9s (attempt 1, $9.00) prompt x",
            f"{stamp(self.recent)} START t-one lane=codex weight=heavy",
            f"{stamp(self.recent)} PASS t-one after 289s (attempt 1, $1.21) prompt f0c tokens in 44",
            f"{stamp(self.recent)} PARKED t-two: PARTIAL: a blind re-run won't change it",
            f"{stamp(self.recent)} PARTIAL t-two after 363s (attempt 1, unknown) prompt 782",
            f"{stamp(self.recent)} runner gate t-one: pass: 2 command(s) exited 0",
            f"{stamp(self.recent)} push t-one: pushed 2 commit(s) to origin/master",
            f"{stamp(self.recent)} push t-three: not pushed (behind)",
            f"{stamp(self.recent)} merge review/t-one: merged",
            "not a log line"]) + "\n")
        w(self.r, ".review/queue/q-auto.md", "# Auto task title\nQueue: auto\n")
        w(self.r, ".review/queue/q-draft.md", "# Draft task title\nQueue: draft\n")
        w(self.r, ".review/parked/t-two.md", "# Parked task title\nQueue: auto\n")
        w(self.r, ".review/parked/t-two.report.md", "RESULT: PARTIAL\n")
        page = w(self.v, "Atlas/Academic/Study Progress.md", "progress\n")
        os.utime(page, (self.recent.timestamp(), self.recent.timestamp()))
        stale = w(self.v, "Atlas/Academic/SEM 1 Modules/Maths/Old.md", "old\n")
        os.utime(stale, (self.old.timestamp(), self.old.timestamp()))
        week = self.v / "Atlas/Academic/SEM 1 Modules/Maths/Maths Week 3.md"
        os.utime(week, (self.recent.timestamp(), self.recent.timestamp()))
        w(self.v, "Dev/digest-state.json", json.dumps({"last": stamp(self.now - dt.timedelta(hours=1))}))

    def tearDown(self):
        self.env.stop()
        from atrium import context, handoff, chatlog, digest
        for m in (context, handoff, chatlog, digest):
            importlib.reload(m)

    def write_cfg(self):
        self.cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        from atrium import context, handoff, chatlog, digest
        importlib.reload(context); importlib.reload(handoff); importlib.reload(chatlog)
        self.d = importlib.reload(digest)
        self.chatlog = chatlog

    def run_digest(self, **kw):
        return self.d.run(repo=self.r, vault=self.v, now=kw.pop("now", self.now), **kw)

    def note(self):
        return self.v / "Calendar" / "Chats" / f"{self.now.date().isoformat()} claude hub.md"

    def handoff(self):
        return self.v / "Calendar" / f"Handoff-{self.now.date().isoformat()}.md"

    # ------------------------------------------------------------------
    def test_digest_collects_every_source_since_the_last_point(self):
        out = self.run_digest()
        self.assertTrue(out.startswith("saved "), out)
        text = self.note().read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: \""))
        self.assertIn("## Laptop digest", text)
        for want in ("- started: t-one", "- passed: t-one", "- parked: t-two", "- partial: t-two",
                     "- pushed: t-one", "- not pushed: t-three", "- merged: t-one",
                     "- spend: $1.21 over 1 run(s)",
                     "tray: fix the mic toggle", "Don: Maths week 3 tutor note",
                     "[[Study Progress]]", "[[Maths Week 3]]",
                     "- waiting: 1 task(s): Auto task title",
                     "approve draft Draft task title", "parked Parked task title"):
            self.assertIn(want, text)
        for gone in ("old-task", "$9.00", "an old repo commit", "vault: an old commit", "[[Old]]",
                     "runner gate", "t-two.report"):
            self.assertNotIn(gone, text)
        order = [text.index(f"### {s['title']}") for s in SHIPPED["digest"]["sections"]]
        self.assertEqual(order, sorted(order))  # the config's section order
        self.assertIn("[atrium] laptop digest saved", self.handoff().read_text(encoding="utf-8"))
        self.assertEqual(json.loads((self.v / "Dev/digest-state.json").read_text())["last"], stamp(self.now))

    def test_only_the_two_notes_are_committed(self):
        w(self.v, "hot.md", "# Hot\nan unsaved edit\n")
        w(self.v, "scratch.md", "untracked\n")
        self.run_digest()
        files = git(self.v, "show", "--name-only", "--format=%s", "HEAD").split("\n")
        self.assertTrue(files[0].startswith("Atrium: laptop digest"))
        self.assertEqual(sorted(f for f in files[1:] if f), sorted([
            f"Calendar/Chats/{self.now.date().isoformat()} claude hub.md",
            f"Calendar/Handoff-{self.now.date().isoformat()}.md"]))
        status = git(self.v, "status", "--porcelain")
        self.assertIn("hot.md", status)
        self.assertIn("scratch.md", status)
        self.assertFalse((self.v / ".obsidian").exists())

    def test_runs_never_repeat_and_quiet_hours_write_nothing(self):
        self.run_digest()
        head = git(self.v, "rev-parse", "HEAD")
        before = self.note().read_text(encoding="utf-8")
        later = self.now + dt.timedelta(minutes=5)
        out = self.run_digest(now=later)  # the queue still has tasks, but that isn't activity
        self.assertIn("nothing written", out)
        self.assertEqual(self.note().read_text(encoding="utf-8"), before)
        self.assertEqual(git(self.v, "rev-parse", "HEAD"), head)  # its own commit isn't news
        self.assertEqual(json.loads((self.v / "Dev/digest-state.json").read_text())["last"], stamp(later))

    def test_dry_run_writes_nothing_and_keeps_the_state(self):
        state = (self.v / "Dev/digest-state.json").read_text()
        out = self.run_digest(dry=True)
        self.assertIn("passed: t-one", out)
        self.assertIn(SHIPPED["digest"]["dry_header"], out)
        self.assertFalse(self.note().exists())
        self.assertFalse(self.handoff().exists())
        self.assertEqual((self.v / "Dev/digest-state.json").read_text(), state)

    def test_since_hours_overrides_the_state(self):
        out = self.run_digest(dry=True, since_hours=6)
        self.assertIn("old-task", out)
        self.assertIn("an old repo commit", out)

    def test_first_run_without_state_uses_the_configured_window(self):
        (self.v / "Dev/digest-state.json").unlink()
        self.cfg["digest"]["first_run_hours"] = 0.1  # six minutes: misses everything from 30 min ago
        self.write_cfg()
        self.assertIn("nothing", self.run_digest(dry=True))

    def test_wording_order_and_limits_come_from_data(self):
        c = self.cfg["digest"]
        c["section_max_lines"] = 2
        c["more"] = "- and {n} others"
        c["section_heading"] = "#### {title}!"
        c["sections"][0]["title"] = "Runner"
        c["sections"] = [c["sections"][1], c["sections"][0]]
        self.write_cfg()
        out = self.run_digest(dry=True)
        self.assertIn("#### Runner!", out)
        self.assertLess(out.index("#### Denisse repo!"), out.index("#### Runner!"))
        self.assertIn("- and 6 others", out)  # 8 runner lines (7 labels + spend), 2 shown
        c["max_lines"] = 1
        self.write_cfg()
        self.assertNotIn("#### Runner!", self.run_digest(dry=True))

    def test_hub_and_laptop_digests_share_the_note_under_their_own_headings(self):
        at = self.now.replace(hour=10, minute=0)
        self.chatlog.save("hub", "hub said this", vault=self.v, now=at)
        self.chatlog.save("atrium", "laptop did that", vault=self.v, now=at.replace(minute=5),
                          since="x", lines=1, sections="Orchestra")
        self.chatlog.save("hub", "hub said more", vault=self.v, now=at.replace(minute=9))
        text = (self.v / "Calendar" / "Chats" / f"{at.date().isoformat()} claude hub.md").read_text(encoding="utf-8")
        self.assertIn("## Saved 10:00\n\nhub said this\n\n## Laptop digest 10:05", text)
        self.assertIn("laptop did that\n\n## Saved 10:09\n\nhub said more\n", text)
        h = self.handoff().read_text(encoding="utf-8")
        self.assertIn("10:00 [claude] hub chat digest saved", h)
        self.assertIn("10:05 [atrium] laptop digest saved (1 line(s): Orchestra)", h)
        self.assertEqual(len(git(self.v, "log", "--format=%s").splitlines()), 5)

    def test_hub_save_cli(self):
        from atrium import __main__ as cli
        md = w(self.tmp, "digest.md", "## what we decided\n- a thing\n")
        with mock.patch("atrium.handoff.VAULT", self.v), mock.patch("builtins.print"):
            self.assertEqual(cli.main(["hub-save", str(md)]), 0)
        self.assertIn("- a thing", self.note().read_text(encoding="utf-8"))

    def test_never_writes_obsidian(self):
        self.cfg["chatlog"]["note"] = ".obsidian/{date}.md"
        self.write_cfg()
        with self.assertRaises(ValueError):
            self.run_digest()
        self.assertFalse((self.v / ".obsidian").exists())

    def test_vault_without_git_still_gets_the_notes(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        w(plain, "Dev/digest-state.json", json.dumps({"last": stamp(self.now - dt.timedelta(hours=1))}))
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)}):
            out = self.d.run(repo=self.r, vault=plain, now=self.now)
        self.assertIn("not committed", out)
        self.assertTrue((plain / "Calendar" / "Chats").is_dir())


class InstallDigestTest(unittest.TestCase):
    def test_prints_the_hourly_task_and_registers_only_when_asked(self):
        from atrium import install_digest as inst
        cmd = inst.command(Path("C:/repo"))
        c = SHIPPED["install_digest"]
        self.assertEqual(cmd[cmd.index("/TN") + 1], c["task_name"])
        self.assertIn("HOURLY", cmd)
        self.assertIn(str(Path("C:/repo") / c["script"]), cmd[cmd.index("/TR") + 1])
        with mock.patch("subprocess.run") as run, mock.patch("builtins.print"):
            self.assertEqual(inst.main([]), 0)
            run.assert_not_called()
            run.return_value.returncode = 0
            inst.main(["--register"])
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
