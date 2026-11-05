"""Regression tests for what the soak test (orchestra/soak, 3 Nov 2026) found in the runner.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_crash_recovery -v

Each test builds a throwaway git repo with a fake lane, so the real queue is never touched.
The bugs, with the seed that reproduces each, are listed in docs/orchestra_soak.md.
"""
# checks: slow  (~20s: real git repos, fake lanes, a tick run in a thread)
import datetime as dt
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Fake lane: optionally writes FAKE_FILE with FAKE_TEXT (UTF-8) and commits it, then reports PASS.
FAKE = textwrap.dedent("""
    import os, sys, time, subprocess, pathlib, json
    sys.stdin.read()
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
    if os.environ.get("FAKE_FILE"):
        p = pathlib.Path(os.environ["FAKE_FILE"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(os.environ.get("FAKE_TEXT", "x"), encoding="utf-8")
        subprocess.run(["git", "add", os.environ["FAKE_FILE"]], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fake"], check=True)
    if not os.environ.get("FAKE_NO_REPORT"):
        pathlib.Path(os.environ["ORCHESTRA_REPORT"]).write_text("RESULT: PASS\\nGATE: ok\\nHUMAN: none\\nPREMISE: right\\n")
    print(json.dumps({"type": "result", "total_cost_usd": 0.01}))
""")
FAKE_REVIEWER = textwrap.dedent("""
    import os, sys, pathlib, json
    prompt = sys.stdin.buffer.read().decode("utf-8", "replace")  # the real reviewer (node) reads UTF-8
    pathlib.Path(os.environ["FAKE_REVIEW_FLAG"]).write_text(prompt, encoding="utf-8")
    print(json.dumps({"type": "result", "result": "VERDICT: ok\\n- fine", "total_cost_usd": 0.01}))
""")
GATE_OK = f'"{sys.executable}" -c "pass"'
# U+201C/U+201D/U+0141: UTF-8 bytes 0x9c, 0x9d, 0x81; the last two are undefined in cp1252. Built with
# chr() so this file stays ASCII: the runner on master reviews this diff with the old decoder.
RIGHT_QUOTE = chr(0x201D)
CURLY = f"{chr(0x201C)}no{RIGHT_QUOTE} {chr(0x141)}"
TASK = """# {slug}
Queue: auto
Priority: normal
Weight: light
Kind: work
Lane: fake
Written: {written}

Do {slug}.

## Gate
```
{gate}
```
"""
ENV = ("FAKE_FILE", "FAKE_TEXT", "FAKE_SLEEP", "FAKE_REVIEW_FLAG", "FAKE_NO_REPORT")


class CrashRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text(".review/*\n.venv\n")
        (self.repo / "orchestra").mkdir()
        shutil.copy(HERE / "review_prompt.md", self.repo / "orchestra" / "review_prompt.md")
        (self.repo / "orchestra" / "notes.txt").write_text("notes\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "init")
        (self.repo / ".venv").mkdir()
        (self.repo / ".review").mkdir()
        self.flag = self.tmp / "reviewed.txt"
        (self.repo / ".review" / "lanes.json").write_text(json.dumps({
            "fake": [sys.executable, "-c", FAKE], "reviewer": [sys.executable, "-c", FAKE_REVIEWER]}))
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_BACKOFF_MIN"] = "0"
        os.environ["ORCHESTRA_DAILY_SPEND_CAP_USD"] = "50"
        os.environ["ORCHESTRA_TIMEOUT_S"] = json.dumps({"light": 20, "heavy": 40})
        os.environ["ORCHESTRA_POWER"] = "ac"
        for k in ENV:
            os.environ.pop(k, None)
        os.environ["FAKE_REVIEW_FLAG"] = str(self.flag)
        import runner
        self.R = importlib.reload(runner)
        self.sleepers = []

    def tearDown(self):
        for p in self.sleepers:
            p.kill()
            p.wait()
        for wt in (self.R.WORKTREES.glob("*") if self.R.WORKTREES.exists() else []):
            self.R.unlink_link(wt / ".venv")
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ENV:
            os.environ.pop(k, None)

    # --- helpers
    def git(self, *a, cwd=None):
        return subprocess.run(["git", *a], cwd=cwd or self.repo, check=True, capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()

    def add(self, slug, gate=GATE_OK):
        p = self.R.QUEUE / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TASK.format(slug=slug, gate=gate, written=dt.datetime.now().strftime("%Y-%m-%dT%H:%M")))
        return p

    def log(self):
        return self.R.LOG_FILE.read_text(encoding="utf-8") if self.R.LOG_FILE.exists() else ""

    def state(self):
        return json.loads(self.R.STATE_FILE.read_text(encoding="utf-8"))

    def sleeper(self, seconds=60):
        p = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
        self.sleepers.append(p)
        time.sleep(0.2)
        return p

    def lock_json(self, pid):
        return json.dumps({"pid": pid, "created": self.R.proc_created(pid), "since": "x"})

    # --- the 2 Nov 2026 incident: a runner bug after PASS killed the tick silently, then re-dispatch

    def test_runner_crash_after_pass_parks_the_task_keeps_the_branch_and_logs_the_traceback(self):
        self.add("t1")
        os.environ["FAKE_FILE"] = "orchestra/notes.txt"  # risky path: the reviewer is called
        os.environ["FAKE_TEXT"] = "changed\n"

        def boom(*a, **k):
            raise RuntimeError("reviewer exploded")
        self.R.run_reviewer = boom
        self.assertEqual(self.R.tick(), "RUNNER-CRASHED")
        self.assertTrue((self.R.PARKED / "t1.md").exists(), "parked, not left in the queue")
        why = (self.R.PARKED / "t1.why.txt").read_text(encoding="utf-8")
        self.assertIn("crashed during review", why)
        self.assertIn("requeue.py t1 --merge-only", why)
        self.assertIn("RUNNER CRASHED t1: RuntimeError during review", self.log())
        self.assertIn("RuntimeError: reviewer exploded", self.log(), "the traceback is in runner.log")
        self.assertEqual(self.git("rev-list", "--count", "main..review/t1"), "1", "the PASSed branch is kept")
        ts = self.state()["tasks"]["t1"]
        self.assertEqual(ts["last_result"], "RUNNER_CRASH")
        self.assertIn("review", ts["history"][-1]["crash"])
        self.assertFalse(self.R.LOCK_FILE.exists(), "the slot lock is released once the task is parked")
        self.assertEqual(self.R.tick(), "idle", "not dispatched again")
        self.assertEqual(self.state()["tasks"]["t1"]["attempts"], 1)

    def test_tick_crash_while_a_task_is_dispatched_is_logged_counted_and_parks_at_the_retry_limit(self):
        self.add("t2")

        def boom(*a, **k):
            raise RuntimeError("lane exploded")
        self.R.run_lane = boom
        with self.assertRaises(RuntimeError):
            self.R.tick()
        log = self.log()
        self.assertIn("TICK CRASHED: RuntimeError: lane exploded", log)
        self.assertIn('raise RuntimeError("lane exploded")', log, "the traceback is in runner.log")
        ts = self.state()["tasks"]["t2"]
        self.assertEqual((ts["attempts"], ts["last_result"]), (1, "RUNNER_CRASH"))
        self.assertEqual(ts["history"][-1]["result"], "RUNNER_CRASH")
        self.assertFalse(self.R.LOCK_FILE.exists())
        self.assertTrue((self.R.QUEUE / "t2.md").exists(), "one retry is still allowed")
        with self.assertRaises(RuntimeError):
            self.R.tick()
        self.assertTrue((self.R.PARKED / "t2.md").exists(), "parked after the retry, like any infra failure")
        self.assertIn("crashed 2 times", (self.R.PARKED / "t2.why.txt").read_text(encoding="utf-8"))

    def test_slot_lock_names_the_task_through_gate_review_and_integration(self):
        check = self.tmp / "check_lock.py"
        check.write_text(textwrap.dedent(f"""
            import json, pathlib, sys
            d = json.loads(pathlib.Path({str(self.R.LOCK_FILE)!r}).read_text(encoding="utf-8"))
            sys.exit(0 if d.get("task") == "t3" and d.get("pid") else 7)
        """), encoding="utf-8")
        self.add("t3", gate=f'"{sys.executable}" "{check}"')  # runs in the worktree, then in the integration tree
        os.environ["FAKE_FILE"] = "orchestra/notes.txt"  # risky: the reviewer runs between the two
        os.environ["FAKE_TEXT"] = "changed\n"
        self.assertEqual(self.R.tick(), "PASS")
        ts = self.state()["tasks"]["t3"]
        self.assertEqual(ts["merge"], "merged")
        self.assertTrue(self.flag.exists(), "the reviewer ran")
        self.assertTrue(any(c.startswith("gate: pass") for c in ts["integration"]["checks"]))
        self.assertFalse(self.R.LOCK_FILE.exists(), "released only after everything")

    def test_a_report_left_by_an_earlier_attempt_is_not_read_as_this_attempts_result(self):
        """This task's own attempt 2 (3 Nov 2026): attempt 1's tick died after its PASS (the incident
        above), so .review/reports/<slug>.md was never archived or renamed. Attempt 2's session wrote
        no report, run_lane cleared only the worktree copy, and finish() read attempt 1's PASS as
        attempt 2's result; the gate then ran in attempt 2's bare worktree and failed."""
        self.add("t8")
        self.R.REPORTS.mkdir(parents=True, exist_ok=True)
        stale = self.R.REPORTS / "t8.md"
        stale.write_text("RESULT: PASS\nGATE: ok\nHUMAN: none\nPREMISE: right\nCOMMITS: attempt 1\n", encoding="utf-8")
        os.environ["FAKE_NO_REPORT"] = "1"
        self.assertEqual(self.R.tick(), "NO_REPORT")
        log = self.log()
        self.assertIn("NO_REPORT t8", log)
        self.assertNotIn("PASS t8", log, "attempt 1's report must not count for attempt 2")
        self.assertFalse(stale.exists(), "the old report is cleared before the lane runs")
        self.assertEqual(self.state()["tasks"]["t8"]["last_result"], "NO_REPORT")
        self.assertTrue((self.R.QUEUE / "t8.md").exists(), "one infra retry, like any NO_REPORT")
        self.assertEqual(list(self.R.ARCHIVE.glob("*/t8*")) if self.R.ARCHIVE.exists() else [], [])

    # --- the cause of the incident: git output decoded with the console code page

    def test_git_output_is_utf8_whatever_the_console_code_page(self):
        (self.repo / "quote.txt").write_text(f"she said {CURLY}\n", encoding="utf-8")
        self.git("add", "quote.txt")
        self.git("commit", "-qm", "quote")
        self.assertEqual(self.R.git("show", "HEAD:quote.txt").stdout, f"she said {CURLY}\n")
        diff = self.R.git("diff", "HEAD~1", "HEAD").stdout
        self.assertIsNotNone(diff, "subprocess's reader thread died decoding and .stdout came back None")
        self.assertIn(RIGHT_QUOTE, diff)

    def test_pass_with_a_curly_quote_in_a_risky_diff_is_reviewed_and_merged(self):
        self.add("t4")
        os.environ["FAKE_FILE"] = "orchestra/notes.txt"
        os.environ["FAKE_TEXT"] = f"{CURLY}\n"  # bytes 0x9d / 0x81: undefined in cp1252
        self.assertEqual(self.R.tick(), "PASS")
        self.assertTrue(self.flag.exists(), "the reviewer saw the diff")
        self.assertIn(RIGHT_QUOTE, self.flag.read_text(encoding="utf-8"))
        self.assertEqual(self.state()["tasks"]["t4"]["merge"], "merged")

    # --- locks

    def test_file_lock_takes_over_a_dead_owner_at_once(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        path = self.R.REVIEW / "state.lock"
        path.write_text(json.dumps({"pid": gone.pid, "created": 1}), encoding="utf-8")
        t0 = time.time()
        with self.R._FileLock(path, wait=10, stale=900):
            self.assertLess(time.time() - t0, 5, "a dead holder must not cost the stale timeout")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pid"], os.getpid())
        self.assertFalse(path.exists())
        self.assertIn("state.lock: holder", self.log())

    def test_file_lock_waits_for_a_live_owner(self):
        other = self.sleeper()
        path = self.R.REVIEW / "merge.lock"
        path.write_text(self.lock_json(other.pid), encoding="utf-8")
        with self.assertRaises(TimeoutError):
            with self.R._FileLock(path, wait=1.2, stale=900):
                pass
        self.assertTrue(path.exists(), "a live holder's lock is never removed")

    @unittest.skipUnless(os.name == "nt", "only Windows refuses to delete an open file")
    def test_unlink_retry_waits_out_a_file_another_process_is_reading(self):
        path = self.R.REVIEW / "runner.fake.lock"
        path.write_text("{}", encoding="utf-8")
        with path.open("r") as held:
            self.assertFalse(self.R.unlink_retry(path, tries=3, pause=0.05), "no exception, just False")
            self.assertTrue(path.exists())
            held.read()
        self.assertTrue(self.R.unlink_retry(path))
        self.assertFalse(path.exists())
        self.assertIn("could not remove runner.fake.lock", self.log())

    def test_dispatch_waits_for_the_dispatch_lock_and_skips_a_task_claimed_meanwhile(self):
        """Two ticks starting together: the scan of other slots' locks must happen under the
        dispatch lock, so the second sees what the first just claimed."""
        self.add("x-first")
        self.add("y-second")
        other = self.sleeper()
        dispatch = self.R.REVIEW / "dispatch.lock"
        dispatch.write_text(self.lock_json(other.pid), encoding="utf-8")  # "another tick is picking"
        result = {}
        th = threading.Thread(target=lambda: result.setdefault("r", self.R.tick()))
        th.start()
        time.sleep(1.5)
        self.assertNotIn("START", self.log(), "nothing dispatched while another tick holds the dispatch lock")
        # The other tick claims x-first in its own slot lock, then lets go of the dispatch lock.
        (self.R.REVIEW / "runner.other.lock").write_text(
            json.dumps({"pid": other.pid, "created": self.R.proc_created(other.pid), "task": "x-first"}), encoding="utf-8")
        dispatch.unlink()
        th.join(timeout=60)
        self.assertFalse(th.is_alive())
        self.assertIn("START y-second", self.log())
        self.assertNotIn("START x-first", self.log())
        self.assertEqual(result.get("r"), "PASS")

    def test_dispatch_rereads_state_so_a_record_saved_meanwhile_is_not_overwritten(self):
        """Another tick saved LIMITED for the task (and limited the lane) after this tick loaded its
        copy of state.json. The dispatch must see that: keep the entry, and not run the task."""
        self.add("s1")
        R = self.R

        def other_tick_saved_meanwhile():
            st = json.loads(R.STATE_FILE.read_text(encoding="utf-8")) if R.STATE_FILE.exists() else R.load_state()
            for k in ("tasks", "days", "done", "lanes"):
                st.setdefault(k, {})
            st.setdefault("durations", {"light": [], "heavy": []})
            task = next(t for t in R.load_queue() if t.slug == "s1")
            ts = st["tasks"].setdefault("s1", {"attempts": 0, "history": [], "digest": task.digest})
            ts["history"].append({"end": "2026-11-03T11:41:20", "result": "LIMITED", "seconds": 2, "lane": "fake",
                                  "weight": "light", "note": "lane fake limited", "spent": "~$0.00 (estimated)"})
            ts.update(last_result="LIMITED", attempts=0)
            st["lanes"]["fake"] = {"limited_until": (dt.datetime.now() + dt.timedelta(hours=1)).isoformat(),
                                   "since": "x", "message": "usage limit reached"}
            R.STATE_FILE.write_text(json.dumps(st), encoding="utf-8")
            return set()
        R.running_tasks = other_tick_saved_meanwhile
        self.assertEqual(R.tick(), "idle", "the lane was limited by then; nothing to run")
        ts = self.state()["tasks"]["s1"]
        self.assertEqual([h["result"] for h in ts["history"]], ["LIMITED"], "the other tick's entry survives")
        self.assertEqual(ts["attempts"], 0)
        self.assertNotIn("START", self.log())

    def test_queue_listing_skips_a_task_file_that_cannot_be_read(self):
        self.add("ok")
        (self.R.QUEUE / "being-moved.md").mkdir()  # a directory: read_text raises an OSError, like a file mid-move
        slugs = [t.slug for t in self.R.load_queue()]
        self.assertEqual(slugs, ["ok"])
        task = self.R.parse_task(self.R.QUEUE / "ok.md")
        task.path = self.R.QUEUE / "being-moved.md"
        self.assertIn("unreadable right now", self.R.header_problem(task))
        self.assertEqual(self.R.tick(), "PASS", "the odd entry doesn't stop the tick")

    @unittest.skipUnless(os.name == "nt", "only Windows refuses to rename an open file")
    def test_move_waits_out_a_file_another_process_is_reading(self):
        src = self.R.QUEUE / "m.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("# m\n", encoding="utf-8")
        held = src.open("r")
        threading.Timer(0.3, held.close).start()
        dest = self.R.move(src, self.R.PARKED)  # ~6 retries, then the rename goes through
        self.assertTrue(dest.exists())
        self.assertFalse(src.exists())

    def test_reconcile_sweeps_a_brief_lock_left_by_a_dead_process(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        for name in ("merge.lock", "dispatch.lock"):
            (self.R.REVIEW / name).write_text(json.dumps({"pid": gone.pid, "created": 1}), encoding="utf-8")
        (self.R.REVIEW / "state.lock").write_text('{"task": "no pid: age rule only"}', encoding="utf-8")
        self.R.reconcile(self.R.load_state())
        self.assertFalse((self.R.REVIEW / "merge.lock").exists())
        self.assertFalse((self.R.REVIEW / "dispatch.lock").exists())
        self.assertTrue((self.R.REVIEW / "state.lock").exists(), "unreadable owner: left to the age rule")
        self.assertIn("RECONCILE merge.lock: holder", self.log())

    def test_merge_already_on_master_is_recovered_recorded_and_archived(self):
        """A tick died between its fast-forward and its state save: master has the merge, state has
        nothing. The recovered run must not call it 'no commits' and leave it unpushed."""
        self.add("t6")
        task = next(t for t in self.R.load_queue() if t.slug == "t6")
        wt, branch = self.R.fresh_worktree(task)
        (wt / "new.txt").write_text("work\n", encoding="utf-8")
        self.git("add", "new.txt", cwd=wt)
        self.git("commit", "-qm", "t6 work", cwd=wt)
        self.git("merge", "--no-ff", "-m", f"orchestra: merge {branch}", branch)  # what the dead tick did
        merged = self.git("rev-parse", "HEAD")
        self.R.REPORTS.mkdir(parents=True, exist_ok=True)
        (self.R.REPORTS / "t6.md").write_text("RESULT: PASS\nGATE: ok\nHUMAN: none\nPREMISE: right\n")
        st = self.R.load_state()
        self.assertEqual(self.R.finish(task, st, wt, branch, "ran", 1, self.R.main_checkout_status()), "PASS")
        ts = st["tasks"]["t6"]
        self.assertEqual(ts["merge"], "merged")
        self.assertEqual(ts["integration"]["commit"], merged)
        self.assertTrue(ts["integration"]["checks"][0].startswith("recovered:"))
        self.assertIn("already on master; recovering", self.log())
        self.assertTrue(any((self.R.ARCHIVE).glob("*/t6.md")), "archived like any merged task")
        self.assertFalse(wt.exists(), "worktree removed like any merged task")
        # And a branch with nothing on it is still just "no commits".
        self.add("t7")
        task7 = next(t for t in self.R.load_queue() if t.slug == "t7")
        wt7, branch7 = self.R.fresh_worktree(task7)
        self.assertEqual(self.R.integrate(task7, branch7, {}), "no commits")

    def test_merge_is_recorded_before_the_push_and_the_archive_moves(self):
        """A tick killed between its fast-forward and its state save (mini soak, seed 2) left a
        merge on master that state never recorded; once the task was archived, reconcile had
        nothing to collect. The record must be on disk the moment master moves."""
        self.add("t9")
        os.environ["FAKE_FILE"] = "new.txt"
        os.environ["FAKE_TEXT"] = "work\n"

        def killed_here(*a, **k):  # the archive move: the first thing after the merge and push
            raise KeyboardInterrupt("tick killed")
        self.R.move = killed_here
        with self.assertRaises(KeyboardInterrupt):
            self.R.tick()
        master = self.git("rev-parse", "HEAD")
        self.assertTrue(self.git("log", "-1", "--format=%s").startswith("orchestra: merge review/t9"))
        ts = self.state()["tasks"]["t9"]
        self.assertEqual(ts["merge"], "merged")
        self.assertEqual(ts["integration"]["commit"], master)
        self.assertEqual(ts["history"][-1]["integration"]["commit"], master)
        self.assertTrue(any(c.startswith("gate: pass") for c in ts["integration"]["checks"]))
        task = next(t for t in self.R.load_queue() if t.slug == "t9")
        self.assertIn(task.digest, self.state()["done"])

    def test_lock_rewrites_are_never_seen_half_written(self):
        """A tick scanning the slots must never read an empty or partial lock while its owner
        rewrites it (write_text truncates first); that hid a running task and it was dispatched twice."""
        self.R.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.R.set_lock_task("busy", "fake")
        seen, stop = [], threading.Event()

        def reader():
            while not stop.is_set():
                info = self.R.read_lock(self.R.LOCK_FILE)
                seen.append(bool(info and info.get("task") == "busy"))
        th = threading.Thread(target=reader)
        th.start()
        try:
            # at least 300 rewrites, and keep going until the reader has had 50 reads (capped)
            i = -1
            while i < 299 or (len(seen) < 50 and i < 20000):
                i += 1
                self.R.update_lock(before=[f"f{i}"] * 20, lane_pid=i)
        finally:
            stop.set()
            th.join()
        self.assertGreater(len(seen), 50, "the reader did run alongside the writes")
        self.assertTrue(all(seen), f"{seen.count(False)} of {len(seen)} reads saw no task")
        self.assertEqual(self.R.read_lock(self.R.LOCK_FILE)["lane_pid"], i)
        self.assertEqual(list(self.R.REVIEW.glob("*.tmp")), [], "no temp file left behind")
        self.R.release_lock()

    def test_empty_env_override_does_not_crash_config_load(self):
        os.environ["ORCHESTRA_VAULT_DIR"] = ""
        try:
            self.assertEqual(self.R.load_config()["vault_dir"], "")
        finally:
            del os.environ["ORCHESTRA_VAULT_DIR"]


if __name__ == "__main__":
    unittest.main()
