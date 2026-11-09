"""Tests for aftercare.py: the vault note and the push's privacy check. Temp dirs only."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[1]))  # testing_git; appended, never shadows
import aftercare  # noqa: E402
import testing_git  # noqa: E402

REPORT = """RESULT: PASS
GATE: tests -> OK
HUMAN: none
PREMISE: right
DONE:
- changed a.py
NOT DONE:
- none
ODD:
- b.py looked unused
COMMITS: abc123
"""
TASK = "# Fix the thing\nQueue: auto\nLane: codex\n\nMake a.py do X.\nKeep b.py.\n\n## Gate\n```\nx\n```\n"


def g(repo, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo,
                          check=True, capture_output=True, text=True)


class TestAftercare(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)  # first: no inherited GIT_* reaches a real repo
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_vault_note_appends_and_commits(self):
        v = self.tmp / "vault"
        v.mkdir()
        g(v, "init", "-q")
        # CI runners have no global git identity; vault_note's own commit needs one (3 Nov 2026:
        # every push failed "Offline regression checks" on this assert)
        g(v, "config", "user.name", "t")
        g(v, "config", "user.email", "t@t")
        for i in (1, 2):
            note = aftercare.vault_note(v, slug=f"s{i}", title="Fix the thing", result="PASS", lane="codex",
                                        seconds=600, spent="$0.00", merge="merged", pushed="pushed 1 commit(s)",
                                        task_text=TASK, report_text=REPORT, report_path="r.md")
        t = note.read_text(encoding="utf-8")
        self.assertTrue(t.startswith("---\nname: Orchestra "))
        self.assertEqual(t.count("## "), 2)
        self.assertIn("**PASS, merged, pushed 1 commit(s)** · lane codex · 10 min", t)
        self.assertIn("Asked: Make a.py do X. Keep b.py.", t)
        self.assertIn("- changed a.py", t)
        self.assertNotIn("Not done", t)  # "none" is left out
        self.assertIn("b.py looked unused", t)
        log = subprocess.run(["git", "log", "--oneline"], cwd=v, capture_output=True, text=True).stdout
        self.assertEqual(len(log.splitlines()), 2)

    def test_privacy_check(self):
        r = self.tmp / "repo"
        r.mkdir()
        g(r, "init", "-q")
        (r / "a.py").write_text("x = 1\n")
        g(r, "add", "a.py")
        g(r, "commit", "-qm", "base")
        g(r, "branch", "up")
        self.assertEqual(aftercare.privacy_problems(r, "up"), [])
        (r / ".env.example").write_text("KEY=\n")
        (r / "memory").mkdir()
        (r / "memory" / ".gitkeep").write_text("")
        g(r, "add", ".")
        g(r, "commit", "-qm", "ok")
        self.assertEqual(aftercare.privacy_problems(r, "up"), [])
        (r / "test_fixture.py").write_text('X = ["-----BEGIN RSA PRIVATE KEY-----\\nabc"]\n')
        g(r, "add", ".")
        g(r, "commit", "-qm", "fixture")
        self.assertEqual(aftercare.privacy_problems(r, "up"), [])  # a bare PEM header isn't a key
        (r / "Atlas").mkdir()
        (r / "Atlas" / "Settled.md").write_text("private\n")
        (r / "b.py").write_text("K = 'sk-" + "a" * 30 + "'\n")
        g(r, "add", ".")
        g(r, "commit", "-qm", "bad")
        p = " ".join(aftercare.privacy_problems(r, "up"))
        self.assertIn("Atlas/Settled.md", p)
        self.assertIn("key or token", p)


HOT = ("---\r\nname: hot\r\n---\r\n# hot\r\n\r\n## Now\r\n- Chuen's line — é\r\n\r\n"
       "## Waiting on Chuen\r\n- Night review wrote this.\r\n- And this.\r\n\r\n## Open hazards\r\n- careful\r\n")


class TestWaitingList(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)  # first: no inherited GIT_* reaches a real repo
        self.tmp = Path(tempfile.mkdtemp())
        self.v = self.tmp / "vault"
        self.v.mkdir()
        g(self.v, "init", "-q")
        g(self.v, "config", "user.name", "t")
        g(self.v, "config", "user.email", "t@t")
        (self.v / "hot.md").write_bytes(HOT.encode("utf-8"))
        g(self.v, "add", "hot.md")
        g(self.v, "commit", "-qm", "base")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commits(self):
        return subprocess.run(["git", "log", "--format=%s"], cwd=self.v, capture_output=True,
                              text=True).stdout.splitlines()

    def outside(self, raw: bytes) -> bytes:
        a = raw.index(aftercare.WAIT_OPEN.encode())
        b = raw.index(aftercare.WAIT_CLOSE.encode()) + len(aftercare.WAIT_CLOSE)
        return raw[:a] + raw[b:]

    def test_created_updated_emptied_and_surroundings_untouched(self):
        self.assertTrue(aftercare.update_waiting(self.v, ["- one"]))
        raw = (self.v / "hot.md").read_bytes()
        self.assertIn(b"## Waiting on Chuen\r\n<!-- orchestra:waiting -->\r\n- one\r\n"
                      b"<!-- /orchestra:waiting -->\r\n- Night review wrote this.\r\n", raw)
        # everything outside the block, minus the one inserted line break, is the original
        self.assertEqual(self.outside(raw).replace(b"Chuen\r\n\r\n", b"Chuen\r\n", 1), HOT.encode("utf-8"))
        first_outside = self.outside(raw)
        self.assertEqual(self.commits()[0], "Orchestra: waiting list")

        # Chuen edits elsewhere; an update changes only the block
        (self.v / "hot.md").write_bytes(raw.replace(b"- careful\r\n", b"- careful\r\n- new by hand\r\n"))
        self.assertTrue(aftercare.update_waiting(self.v, ["- two", "- three"]))
        raw2 = (self.v / "hot.md").read_bytes()
        self.assertIn(b"<!-- orchestra:waiting -->\r\n- two\r\n- three\r\n<!-- /orchestra:waiting -->", raw2)
        self.assertNotIn(b"- one\r\n", raw2)
        self.assertEqual(self.outside(raw2), first_outside.replace(b"- careful\r\n", b"- careful\r\n- new by hand\r\n"))
        self.assertEqual(raw2.count(aftercare.WAIT_OPEN.encode()), 1)

        n = len(self.commits())
        self.assertFalse(aftercare.update_waiting(self.v, ["- two", "- three"]))  # unchanged: no commit
        self.assertEqual(len(self.commits()), n)

        self.assertTrue(aftercare.update_waiting(self.v, []))  # emptied
        raw3 = (self.v / "hot.md").read_bytes()
        self.assertIn(b"<!-- orchestra:waiting -->\r\n<!-- /orchestra:waiting -->", raw3)
        self.assertEqual(self.outside(raw3), self.outside(raw2))
        self.assertEqual(len(self.commits()), n + 1)
        changed = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=self.v,
                                 capture_output=True, text=True).stdout.split()
        self.assertEqual(changed, ["hot.md"])

    def test_only_hot_md_is_committed(self):
        (self.v / "other.md").write_text("Chuen's uncommitted note\n")
        g(self.v, "add", "other.md")  # staged by someone else: must stay staged, not committed
        aftercare.update_waiting(self.v, ["- x"])
        changed = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=self.v,
                                 capture_output=True, text=True).stdout.split()
        self.assertEqual(changed, ["hot.md"])
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=self.v,
                                capture_output=True, text=True).stdout.split()
        self.assertEqual(staged, ["other.md"])

    def test_no_heading_and_no_file(self):
        self.assertEqual(aftercare._render_hot("# hot\n- a", ["- x"]),
                         "# hot\n- a\n\n## Waiting on Chuen\n<!-- orchestra:waiting -->\n- x\n<!-- /orchestra:waiting -->\n")
        self.assertEqual(aftercare._render_hot("", []),
                         "## Waiting on Chuen\n<!-- orchestra:waiting -->\n<!-- /orchestra:waiting -->\n")

    def test_waiting_lines(self):
        import datetime as dt
        q, p = self.tmp / "queue", self.tmp / "parked"
        q.mkdir()
        p.mkdir()
        (q / "d1.md").write_text("# Draft one\nQueue: draft\nLane: claude\n", encoding="utf-8")
        (q / "a1.md").write_text("# Auto one\nQueue: auto\n", encoding="utf-8")
        (p / "k1.md").write_text("# Parked one\nQueue: auto\n", encoding="utf-8")
        (p / "k1.why.txt").write_text("2026-11-02T23:40:00 waiting on Chuen: sign in?\nTo run it again: ...\n",
                                      encoding="utf-8")
        state = {"tasks": {"m1": {"merge": "merge conflict; merge by hand"}, "m2": {"merge": "merged"}},
                 "lanes": {"claude": {"limited_until": "2026-11-03T00:20:00"},
                           "codex": {"limited_until": "2026-11-02T10:00:00"}}}
        out = aftercare.waiting_lines(queue_dir=q, parked_dir=p, state=state, now=dt.datetime(2026, 11, 2, 23, 50))
        self.assertEqual(len(out), 4, out)
        self.assertIn("Draft one", out[0])
        self.assertIn("`Queue: draft` → `Queue: auto`", out[0])
        self.assertIn(str(q / "d1.md"), out[0])
        self.assertIn("Parked one", out[1])
        self.assertIn("waiting on Chuen: sign in?", out[1])
        self.assertNotIn("To run it again", out[1])
        self.assertIn("`review/m1` — merge conflict; merge by hand", out[2])
        self.assertEqual(out[3], "- Lane claude limited until 2026-11-03 00:20")
        self.assertEqual(aftercare.waiting_lines(queue_dir=self.tmp / "nope", parked_dir=self.tmp / "nope",
                                                 state={}, now=dt.datetime(2026, 11, 2)), [])


class TestParallelState(unittest.TestCase):
    def test_two_lanes_merge_their_changes(self):
        import runner
        base = {"tasks": {"a": {"attempts": 0}}, "done": {}, "durations": {"light": [5], "heavy": []},
                "days": {"d": {"work_s": 100, "cost_usd": 1.0}}}
        other = {"tasks": {"a": {"attempts": 0}, "b": {"attempts": 1}}, "done": {"hb": "t"},
                 "durations": {"light": [5, 7], "heavy": []}, "days": {"d": {"work_s": 160, "cost_usd": 1.5}}}
        mine = {"tasks": {"a": {"attempts": 1}}, "done": {"ha": "t"},
                "durations": {"light": [5], "heavy": [900]}, "days": {"d": {"work_s": 400, "cost_usd": 3.0}}}
        out = runner._merge_into(other, mine, base)
        self.assertEqual(out["tasks"], {"a": {"attempts": 1}, "b": {"attempts": 1}})
        self.assertEqual(out["done"], {"hb": "t", "ha": "t"})
        self.assertEqual(out["durations"], {"light": [5, 7], "heavy": [900]})
        self.assertEqual(out["days"]["d"], {"work_s": 460, "cost_usd": 3.5})


if __name__ == "__main__":
    unittest.main(verbosity=2)
