"""Tests for safe_json.py: damaged files are kept aside, saves are atomic,
the cross-process lock loses nothing and a dead holder's lock is taken over.

Everything runs in temp folders; the subprocesses import safe_json from
this checkout.
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Run on its own (not through run_checks, which sets a temp vault), the
# history and facts cases must still never reach the real vault.
os.environ.setdefault("DENISSE_VAULT", tempfile.mkdtemp(prefix="safe_json_vault_"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import safe_json

logging.getLogger("denisse.safe_json").setLevel(logging.ERROR)


def _run(code, *args, wait=True):
    cmd = [sys.executable, "-c", textwrap.dedent(code), str(ROOT), *map(str, args)]
    if wait:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "data.json")

    def tearDown(self):
        self._tmp.cleanup()


class Reading(Base):
    def test_missing_file_is_default_and_nothing_written(self):
        self.assertEqual(safe_json.read_json(self.path, []), [])
        self.assertEqual(os.listdir(self.dir), [])

    def test_good_file_round_trips(self):
        data = {"a": [1, 2, {"b": "café"}], "n": None}
        safe_json.write_json_atomic(self.path, data, indent=2, ensure_ascii=False)
        self.assertEqual(safe_json.read_json(self.path, {}), data)

    def test_truncated_file_kept_aside_and_default_returned(self):
        whole = json.dumps([{"role": "user", "content": "x" * 50}] * 5)
        torn = whole[: len(whole) // 2].encode()
        with open(self.path, "wb") as f:
            f.write(torn)
        self.assertEqual(safe_json.read_json(self.path, []), [])
        copies = safe_json.corrupt_copies(self.path)
        self.assertEqual(len(copies), 1)
        with open(copies[0], "rb") as f:
            self.assertEqual(f.read(), torn)
        name = os.path.basename(copies[0])
        self.assertTrue(name.startswith("data.corrupt-") and name.endswith(".json"), name)
        # The damaged file itself is untouched until the caller saves.
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), torn)

    def test_empty_file_counts_as_damaged(self):
        open(self.path, "wb").close()
        self.assertEqual(safe_json.read_json(self.path, {"k": 1}), {"k": 1})
        self.assertEqual(len(safe_json.corrupt_copies(self.path)), 1)

    def test_same_damage_kept_once(self):
        with open(self.path, "w") as f:
            f.write("[1, 2")
        for _ in range(3):
            safe_json.read_json(self.path, [])
        self.assertEqual(len(safe_json.corrupt_copies(self.path)), 1)

    def test_save_after_damage_keeps_the_copy(self):
        with open(self.path, "w") as f:
            f.write('{"half": ')
        data = safe_json.read_json(self.path, {})
        data["new"] = 1
        safe_json.write_json_atomic(self.path, data)
        self.assertEqual(safe_json.read_json(self.path, {}), {"new": 1})
        with open(safe_json.corrupt_copies(self.path)[0]) as f:
            self.assertEqual(f.read(), '{"half": ')

    def test_corrupt_name_comes_from_config(self):
        old = dict(safe_json.configure())
        try:
            safe_json.CONFIG = dict(old)
            safe_json.CONFIG["corrupt_name"] = "{stem}{ext}.broken-{stamp}"
            with open(self.path, "w") as f:
                f.write("{")
            safe_json.read_json(self.path, None)
            self.assertEqual(len([n for n in os.listdir(self.dir) if ".broken-" in n]), 1)
        finally:
            safe_json.CONFIG = old


class Writing(Base):
    def test_save_killed_between_temp_and_replace_leaves_old_file(self):
        old = [{"id": i} for i in range(20)]
        safe_json.write_json_atomic(self.path, old)
        # A real kill: the child writes its temp file, then dies at os.replace.
        r = _run("""
            import os, sys
            sys.path.insert(0, sys.argv[1])
            import safe_json
            os.replace = lambda *a, **k: os._exit(9)
            safe_json.write_json_atomic(sys.argv[2], ["new"] * 1000)
        """, self.path)
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertEqual(safe_json.read_json(self.path, None), old)
        self.assertEqual(safe_json.corrupt_copies(self.path), [])

    def test_failed_replace_cleans_its_temp_file(self):
        safe_json.write_json_atomic(self.path, [1])
        real = os.replace

        def boom(*a, **k):
            raise OSError("disk gone")
        os.replace = boom
        try:
            with self.assertRaises(OSError):
                safe_json.write_json_atomic(self.path, [2])
        finally:
            os.replace = real
        self.assertEqual(os.listdir(self.dir), ["data.json"])
        self.assertEqual(safe_json.read_json(self.path, None), [1])

    def test_replace_retries_permission_error(self):
        real = os.replace
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("held by a reader")
            return real(*a, **k)
        os.replace = flaky
        try:
            safe_json.write_json_atomic(self.path, {"ok": True})
        finally:
            os.replace = real
        self.assertEqual(len(calls), 3)
        self.assertEqual(safe_json.read_json(self.path, None), {"ok": True})


APPENDER = """
    import sys
    sys.path.insert(0, sys.argv[1])
    import safe_json
    path, tag, n = sys.argv[2], sys.argv[3], int(sys.argv[4])
    for i in range(n):
        with safe_json.locked(path):
            items = safe_json.read_json(path, [])
            items.append(f"{tag}-{i}")
            safe_json.write_json_atomic(path, items)
"""


class Locking(Base):
    def test_two_processes_appending_lose_nothing(self):
        n = 200
        procs = [_run(APPENDER, self.path, tag, n, wait=False) for tag in ("a", "b")]
        for p in procs:
            out, err = p.communicate(timeout=300)
            self.assertEqual(p.returncode, 0, err)
        items = safe_json.read_json(self.path, None)
        self.assertEqual(len(items), 2 * n)
        self.assertEqual(sorted(items), sorted([f"{t}-{i}" for t in "ab" for i in range(n)]))
        self.assertEqual(safe_json.corrupt_copies(self.path), [])

    def test_stale_lock_from_dead_process_is_taken_over(self):
        # The child takes the lock and dies holding it: the lock file stays.
        r = _run("""
            import os, sys
            sys.path.insert(0, sys.argv[1])
            import safe_json
            cm = safe_json.locked(sys.argv[2])
            cm.__enter__()
            os._exit(0)
        """, self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(safe_json.lock_path(self.path)))
        start = time.monotonic()
        with safe_json.locked(self.path):
            safe_json.write_json_atomic(self.path, [1])
        self.assertLess(time.monotonic() - start, safe_json._cfg("lock_wait_seconds") / 2)

    def test_live_holder_blocks_another_process(self):
        holder = _run("""
            import sys, time
            sys.path.insert(0, sys.argv[1])
            import safe_json
            with safe_json.locked(sys.argv[2]):
                print("held", flush=True)
                time.sleep(1.5)
        """, self.path, wait=False)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        start = time.monotonic()
        with safe_json.locked(self.path):
            waited = time.monotonic() - start
        holder.communicate(timeout=60)
        self.assertGreater(waited, 0.5)

    def test_reentrant_in_one_thread(self):
        with safe_json.locked(self.path):
            with safe_json.locked(self.path):
                safe_json.write_json_atomic(self.path, [1])
            safe_json.write_json_atomic(self.path, [2])
        self.assertEqual(safe_json.read_json(self.path, None), [2])

    def test_threads_take_turns(self):
        import threading

        def work(tag):
            for i in range(50):
                with safe_json.locked(self.path):
                    items = safe_json.read_json(self.path, [])
                    items.append(f"{tag}{i}")
                    safe_json.write_json_atomic(self.path, items)
        ts = [threading.Thread(target=work, args=(t,)) for t in "xyz"]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(safe_json.read_json(self.path, [])), 150)


class Callers(Base):
    """memory_store (the history) and tools (facts) through safe_json.
    test_conversation.py guards record()/replay() and is left as it is."""

    def setUp(self):
        super().setUp()
        import memory_store
        import obsidian_sync
        import tools
        self.ms, self.tools, self.sync = memory_store, tools, obsidian_sync
        self._saved = (memory_store.MEMORY_DIR, memory_store.MEMORY_FILE, tools.FACTS_FILE,
                       obsidian_sync.sync_conversation, obsidian_sync.sync_facts)
        memory_store.MEMORY_DIR = self.dir
        memory_store.MEMORY_FILE = os.path.join(self.dir, "conversation.json")
        tools.FACTS_FILE = os.path.join(self.dir, "facts.json")
        obsidian_sync.sync_conversation = lambda *a, **k: None
        obsidian_sync.sync_facts = lambda *a, **k: None
        import instincts
        self.instincts, self._rebuild = instincts, instincts.rebuild
        instincts.rebuild = lambda *a, **k: None  # it writes the vault's Brain folder

    def tearDown(self):
        (self.ms.MEMORY_DIR, self.ms.MEMORY_FILE, self.tools.FACTS_FILE,
         self.sync.sync_conversation, self.sync.sync_facts) = self._saved
        self.instincts.rebuild = self._rebuild
        super().tearDown()

    def test_history_round_trips_unchanged(self):
        history = [{"role": "user", "content": "hey", "turn_id": "t1"},
                   {"role": "assistant", "content": "Doing alright, you?"}]
        with open(self.ms.MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        with open(self.ms.MEMORY_FILE, "rb") as f:
            before = f.read()
        self.assertEqual(self.ms.load_history(), history)
        self.ms.save_history(self.ms.load_history())
        with open(self.ms.MEMORY_FILE, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_torn_history_kept_aside_and_next_record_proceeds(self):
        import conversation
        torn = '[{"role": "user", "content": "remember the supervisor meeting"}, {"role": "assis'
        with open(self.ms.MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write(torn)
        self.assertEqual(self.ms.load_history(), [])
        self.assertTrue(conversation.record([{"role": "user", "content": "hi"},
                                             {"role": "assistant", "content": "hello"}]))
        self.assertEqual([m["content"] for m in self.ms.load_history()], ["hi", "hello"])
        with open(safe_json.corrupt_copies(self.ms.MEMORY_FILE)[0], encoding="utf-8") as f:
            self.assertEqual(f.read(), torn)

    def test_history_appends_from_two_processes_lose_none(self):
        code = """
            import sys
            sys.path.insert(0, sys.argv[1])
            import memory_store, obsidian_sync, os
            obsidian_sync.sync_conversation = lambda *a, **k: None
            memory_store.MEMORY_DIR = os.path.dirname(sys.argv[2])
            memory_store.MEMORY_FILE = sys.argv[2]
            for i in range(int(sys.argv[4])):
                with memory_store.locked():
                    h = memory_store.load_history()
                    h.append({"role": "user", "content": f"{sys.argv[3]}{i}"})
                    memory_store.save_history(h)
        """
        n = 60  # under MAX_STORED_MESSAGES, so trimming can't hide a loss
        procs = [_run(code, self.ms.MEMORY_FILE, tag, n, wait=False) for tag in "ab"]
        for p in procs:
            _, err = p.communicate(timeout=300)
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual(len(self.ms.load_history()), 2 * n)

    def test_torn_facts_kept_aside(self):
        with open(self.tools.FACTS_FILE, "w", encoding="utf-8") as f:
            f.write('{"favorite_coffee": "flat wh')
        self.tools.remember("editor", "vscode")
        self.assertEqual(self.tools._load_facts(), {"editor": "vscode"})
        self.assertEqual(len(safe_json.corrupt_copies(self.tools.FACTS_FILE)), 1)


class Config(unittest.TestCase):
    def test_live_config_matches_defaults_keys(self):
        with open(safe_json.DEFAULTS_FILE, encoding="utf-8") as f:
            defaults = {k for k in json.load(f) if not k.startswith("_")}
        self.assertEqual(set(safe_json.configure()), defaults)


if __name__ == "__main__":
    unittest.main()
