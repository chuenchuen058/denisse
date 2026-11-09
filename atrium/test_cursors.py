"""python -m unittest atrium.test_cursors -v

Agent cursors on the live page (atrium/cursors.py): a temp main repo with real
`git worktree`s under a temp worktrees folder, one per running task, and a temp .review
whose runner locks say who runs what. Touched files are collected per lane (committed,
uncommitted, untracked), ignored paths drop out, a file two agents touch gives exactly one
warning (also in Needs you), idle lanes give no cursor, and nothing is written anywhere.
Hermetic git (testing_git); no network, no Ollama, no real vault."""
import copy
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
WORDS = SHIPPED["live"]["words"]


def w(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def tree(root: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(root.rglob("*")) if p.is_file()}


class CursorsTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)  # first: no inherited GIT_* reaches a real repo
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = testing_git.init_repo(self.tmp / "denisse", "-b", "master")
        self.wts = self.tmp / "denisse-wt"
        self.review, self.vault = self.tmp / "review", self.tmp / "vault"
        self.review.mkdir(), self.vault.mkdir()
        for rel in ("orchestra/runner.py", "atrium/live.py", "README.md", "eval/cases.py"):
            w(self.repo, rel, "base\n")
        w(self.repo, "orchestra/config.json", json.dumps({"conflict_ignore_prefixes": [".venv/"],
                                                           "lane_slots": {"claude": 2, "copilot": 1, "codex": 1}}))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.cfg = copy.deepcopy(SHIPPED)
        self.cfg["live"].update(port=0, refresh_s=3600)
        self.write_cfg()
        for p in (mock.patch.dict(os.environ, {"DENISSE_VAULT": str(self.vault),
                                                "ATRIUM_CONFIG": str(self.tmp / "atrium.json"),
                                                "ORCHESTRA_POWER": "ac"}),
                  mock.patch("urllib.request.urlopen", side_effect=OSError("no network in tests"))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._reload)
        from atrium import context, snapshot, cursors, live
        importlib.reload(context)
        self.snap = importlib.reload(snapshot)
        self.snap.REPO, self.snap.REVIEW = self.repo, self.review
        self.cur = importlib.reload(cursors)
        self.live = importlib.reload(live)

    @staticmethod
    def _reload():
        from atrium import context, snapshot, cursors
        importlib.reload(context), importlib.reload(snapshot), importlib.reload(cursors)

    def write_cfg(self):
        (self.tmp / "atrium.json").write_text(json.dumps(self.cfg), encoding="utf-8")

    def lane(self, lane, slug, slot=1, commit=(), edit=(), new=()):
        """A running task: its runner lock, its queue file and a real worktree with work in it."""
        wt = self.wts / slug
        git(self.repo, "worktree", "add", "-q", "-b", f"review/{slug}", str(wt))
        for rel in commit:
            w(wt, rel, f"{slug} committed\n")
        if commit:
            git(wt, "add", *commit)
            git(wt, "commit", "-qm", "work")
        for rel in edit:
            w(wt, rel, f"{slug} editing\n")
        for rel in new:
            w(wt, rel, "new\n")
        name = f"runner.{lane}.lock" if slot == 1 else f"runner.{lane}.{slot}.lock"
        w(self.review, name, json.dumps({"task": slug, "since": "2026-11-09T20:00:00"}))
        w(self.review, f"queue/{slug}.md", f"# Task {slug}\nLane: {lane}\nQueue: auto\n\nDo it.\n")
        return wt

    def where(self):
        return self.live.collect()["where"]

    # -------------------------------------------------------------------------------
    def test_touched_files_are_collected_per_lane(self):
        self.lane("claude", "t-a", commit=["orchestra/runner.py"], edit=["README.md"], new=["atrium/new.py"])
        self.lane("copilot", "t-b", edit=["eval/cases.py"])
        d = self.where()
        files = {f["path"]: f["who"] for f in d["files"]}
        self.assertEqual(files, {"orchestra/runner.py": ["claude:1"], "README.md": ["claude:1"],
                                 "atrium/new.py": ["claude:1"], "eval/cases.py": ["copilot:1"]})
        k = {c["id"]: c for c in d["cursors"]}
        self.assertEqual(k["claude:1"]["task_id"], "t-a")
        self.assertEqual(k["claude:1"]["label"], "Claude")
        self.assertEqual(k["copilot:1"]["file"], "eval/cases.py")
        self.assertIn(k["claude:1"]["file"], files)
        self.assertEqual(d["overlaps"], [])

    def test_a_shared_file_gives_one_warning_and_shows_in_needs_you(self):
        self.lane("claude", "t-a", commit=["orchestra/runner.py"], edit=["README.md"])
        self.lane("copilot", "t-b", edit=["orchestra/runner.py", "README.md"])
        self.lane("claude", "t-c", slot=2, edit=["orchestra/runner.py"])
        d = self.live.collect()
        ov = {o["path"]: o for o in d["where"]["overlaps"]}
        self.assertEqual(sorted(ov), ["README.md", "orchestra/runner.py"])  # one per shared file
        self.assertEqual(ov["orchestra/runner.py"]["who"], ["claude:1", "claude:2", "copilot:1"])
        two = WORDS["both_in"].format(who=WORDS["both_join"].join(["Claude 1", "Copilot"]), path="README.md")
        self.assertEqual(ov["README.md"]["text"], two)
        files = d["where"]["files"]
        self.assertTrue(files[0]["shared"] and files[1]["shared"])  # shared files first
        self.assertEqual({f["path"] for f in files if f["shared"]}, set(ov))
        overlap_needs = [n for n in d["needs"] if n["kind"] == "overlap"]
        self.assertEqual([n["item"] for n in overlap_needs],
                         [o["text"] for o in d["where"]["overlaps"]][:SHIPPED["live"]["needs_max"]])
        self.assertEqual(overlap_needs[0]["icon"], SHIPPED["live"]["needs_icons"]["overlap"])
        self.assertEqual(d["needs"][0]["kind"], "overlap")

    def test_idle_lanes_give_no_cursor(self):
        self.lane("claude", "t-a", edit=["README.md"])
        running = self.snap.tables(["running"])["running"]
        self.assertTrue(any(not r["task"] for r in running), "the fixture has idle slots")
        d = self.where()
        self.assertEqual([c["id"] for c in d["cursors"]], ["claude:1"])
        for r in running:
            r["task"] = ""
        self.assertEqual(self.cur.collect(running, [], 0, WORDS)["cursors"], [])

    def test_a_running_task_without_a_worktree_has_a_cursor_but_no_files(self):
        w(self.review, "runner.codex.lock", json.dumps({"task": "t-none", "since": "2026-11-09T20:00:00"}))
        d = self.where()
        self.assertEqual([(c["id"], c["file"], c["files"]) for c in d["cursors"]], [("codex:1", "", 0)])
        self.assertEqual(d["files"], [])

    def test_ignored_paths_are_filtered(self):
        self.lane("claude", "t-a", edit=["README.md"],
                  new=[".review/report.md", "atrium/__pycache__/live.cpython-312.pyc", "stray.pyc",
                       ".venv/lib.py", "runner.log", "keep/me.py"])
        self.assertEqual(sorted(f["path"] for f in self.where()["files"]), ["README.md", "keep/me.py"])
        self.cfg["live"]["cursors"]["use_runner_ignores"] = False  # .venv/ was the runner's rule, not ours
        self.cfg["live"]["cursors"]["ignore"].append("keep/*")
        self.write_cfg()
        self.cur._cache.clear()
        self.assertEqual(sorted(f["path"] for f in self.where()["files"]), [".venv/lib.py", "README.md"])

    def test_max_files_and_page_sizes_come_from_config(self):
        self.lane("claude", "t-a", new=[f"f{i}.py" for i in range(5)])
        self.cfg["live"]["cursors"].update(max_files=2, arrow_px=21)
        self.write_cfg()
        d = self.where()
        self.assertEqual(len(d["files"]), 2)
        self.assertEqual(d["more_files"], WORDS["more_files"].format(n=3))
        self.assertEqual(d["cursor_cfg"]["arrow_px"], 21)
        k = d["cursors"][0]
        self.assertTrue(k["file"] == "" or k["file"] in {f["path"] for f in d["files"]})

    def test_files_are_cached_for_the_refresh_interval(self):
        self.lane("claude", "t-a", edit=["README.md"])
        self.where()
        with mock.patch.object(self.cur.subprocess, "run", wraps=subprocess.run) as run:
            self.where()
            self.assertEqual(run.call_count, 0)  # inside refresh_s: no git at all
            self.cfg["live"]["refresh_s"] = 0
            self.write_cfg()
            self.where()
            self.assertGreater(run.call_count, 0)

    def test_git_runs_without_inherited_git_vars_and_without_locks(self):
        self.lane("claude", "t-a", edit=["README.md"])
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.tmp / "elsewhere")}), \
                mock.patch.object(self.cur.subprocess, "run", wraps=subprocess.run) as run:
            self.assertEqual([f["path"] for f in self.where()["files"]], ["README.md"])
        for call in run.call_args_list:
            env = call.kwargs["env"]
            self.assertNotIn("GIT_DIR", env)
            self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
            self.assertTrue(set(call.args[0][1:]) & {"merge-base", "diff", "ls-files"})  # read commands only

    def test_nothing_is_written(self):
        self.lane("claude", "t-a", commit=["orchestra/runner.py"], edit=["README.md"], new=["x.py"])
        self.lane("copilot", "t-b", edit=["README.md"])
        before = tree(self.tmp)
        self.live.collect()
        self.cur._cache.clear()
        self.live.collect()
        self.assertEqual(tree(self.tmp), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
