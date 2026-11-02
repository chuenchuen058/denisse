"""Tests for the local team with a fake Ollama (a local HTTP stub) and a temp repo: the scout
on fixture logs, planner output validation, the librarian, the producer, skips, and the
GPU lease. No real model is called and nothing is pulled.

    .venv\\Scripts\\python.exe -m unittest discover -s orchestra/local_team -p "test_*.py" -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from orchestra.local_team import core, roles  # noqa: E402
from orchestra.local_team import __main__ as cli  # noqa: E402
from orchestra.local_team.lease import GpuLease, main_checkout  # noqa: E402

MODEL = "llama3.1:8b"


class FakeOllama:
    """/api/tags, /api/ps, /api/chat. `answers` maps a role name to a list of replies (dicts
    are JSON-encoded, strings sent as-is); every request is recorded."""

    def __init__(self, models=(MODEL,), loaded=()):
        self.models, self.loaded = list(models), list(loaded)
        self.answers: dict[str, list] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                if self.path == "/api/tags":
                    self._send({"models": [{"name": m} for m in fake.models]})
                elif self.path == "/api/ps":
                    self._send({"models": [{"name": m} for m in fake.loaded]})
                else:
                    self._send({"error": "no"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                fake.requests.append(("POST", self.path, body))
                if self.path != "/api/chat":
                    return self._send({"error": "not allowed in tests"}, 404)
                role = body["messages"][0]["content"]
                name = next(n for n in ("scout", "planner", "librarian") if f"You are the {n}" in role)
                queue = fake.answers.get(name) or [{}]
                ans = queue.pop(0) if len(queue) > 1 else queue[0]
                self._send({"model": body["model"], "message": {"role": "assistant",
                            "content": ans if isinstance(ans, str) else json.dumps(ans)}, "done": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def chats(self, role=None):
        out = [b for m, p, b in self.requests if p == "/api/chat"]
        return [b for b in out if role is None or f"You are the {role}" in b["messages"][0]["content"]]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


RUNNER_LOG = """2026-11-02T19:00:00 START 2026-11-02T1900-a lane claude
2026-11-02T19:20:00 PASS 2026-11-02T1900-a after 1200s
2026-11-02T19:21:00 merge review/2026-11-02T1900-a: merged
2026-11-02T19:30:00 PARKED 2026-11-02T1930-voice-thing: PARTIAL: a blind re-run won't change it (see report)
2026-11-02T19:40:00 runner gate 2026-11-02T1940-b: broken: exit 5 (pytest found no tests)
"""
REPORT = """RESULT: PARTIAL
GATE: .venv\\Scripts\\python.exe -m unittest orchestra.test_runner -v -> exit 0; does not cover: battery
HUMAN: none
PREMISE: right
DONE:
- runner.py: reconcile() collects orphaned lanes
NOT DONE:
- No kill/timeout for an orphan lane that never exits
ODD:
- none
COMMITS: abc123
"""


class LocalTeamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        r = self.repo
        for d in ("orchestra/agent_hub", "scripts", ".review/queue", ".review/parked",
                  ".review/archive/2026-11-02", "vault"):
            (r / d).mkdir(parents=True)
        shutil.copy(REPO_ROOT / "orchestra" / "gates.json", r / "orchestra" / "gates.json")
        cfg = json.loads((REPO_ROOT / "orchestra" / "config.json").read_text(encoding="utf-8"))
        cfg["vault_dir"] = str(r / "vault")
        (r / "orchestra" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        for f in ("orchestra/runner.py", "orchestra/test_runner.py", "scripts/test_tasks.py"):
            (r / f).write_text("# stub\n", encoding="utf-8")
        (r / ".review" / "runner.log").write_text(RUNNER_LOG, encoding="utf-8")
        (r / ".review" / "parked" / "2026-11-02T1930-voice-thing.why.txt").write_text(
            "2026-11-02T19:30:00 PARTIAL: a blind re-run won't change it (see report)\nTo run it again: ...\n",
            encoding="utf-8")
        (r / ".review" / "archive" / "2026-11-02" / "2026-11-02T1800-reconcile.report.md").write_text(
            REPORT, encoding="utf-8")
        (r / ".review" / "checks.json").write_text(json.dumps({"results": [
            {"suite": "scripts/test_tasks.py", "passed": False, "exit_code": 1},
            {"suite": "scripts/test_ok.py", "passed": True, "exit_code": 0}]}, indent=2), encoding="utf-8")
        self.fake = FakeOllama()
        self.ctx = core.Ctx(r, ollama=self.fake.url)
        self.gpu = mock.patch.object(core, "gpu_utilisation", return_value=None)
        self.gpu.start()
        # The CLI builds its own Ctx: point it at the fake, never at the real Ollama.
        self.env = mock.patch.dict(os.environ, {"OLLAMA_HOST_URL": self.fake.url})
        self.env.start()
        os.environ.pop("ORCHESTRA_GPU_LEASE", None)

    def tearDown(self):
        self.gpu.stop()
        self.env.stop()
        self.fake.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scout_answer(self, ids=("E1",), extra_bad=True):
        cands = [{"problem": "Gate re-run found no tests (exit 5) for task b; its gate is broken.",
                  "evidence": list(ids), "area_hint": "orchestra", "worth": "high"}]
        if extra_bad:
            cands.append({"problem": "Invented problem citing evidence that does not exist.",
                          "evidence": ["E99"], "area_hint": "", "worth": "high"})
        return {"candidates": cands, "nothing_because": ""}

    def planner_answer(self, **over):
        a = {"title": "Runner flags a gate that finds no tests before merging",
             "objective": "A gate exiting 5 (no tests) is broken. Make the runner say so clearly in the park reason.",
             "benefit": "Chuen sees why a task parked without opening logs.",
             "scope_paths": ["orchestra/runner.py", "orchestra/test_runner.py"],
             "acceptance": ["A gate exiting 5 parks the task with 'broken gate' in the reason."],
             "gate": {"id": "unittest", "arg": "orchestra.test_runner"}, "weight": "light"}
        a.update(over)
        return a

    # ------------------------------------------------------------ scout

    def test_scout_reads_the_bounded_delta_and_drops_invented_evidence(self):
        self.fake.answers["scout"] = [self.scout_answer(ids=("E1", "E2"))]
        out = roles.scout(self.ctx)
        texts = [e["text"] for e in out["evidence"]]
        self.assertTrue(any("PARKED 2026-11-02T1930-voice-thing" in t for t in texts))
        self.assertTrue(any("broken: exit 5" in t for t in texts))
        self.assertFalse(any(" START " in t or " PASS " in t or "merge review" in t for t in texts))
        self.assertIn("No kill/timeout for an orphan lane that never exits", texts)
        self.assertNotIn("none", [t.lower() for t in texts])          # ODD: none is not evidence
        self.assertIn("scripts/test_tasks.py", texts)                 # failing suite
        self.assertNotIn("scripts/test_ok.py", texts)                 # passing suite
        for e in out["evidence"]:                                     # every quote is checkable later
            self.assertIn(e["text"], (self.repo / e["source"]).read_text("utf-8"))
        self.assertEqual(len(out["candidates"]), 1)                   # the E99 one was dropped
        self.assertEqual([e["id"] for e in out["candidates"][0]["evidence_items"]], ["E1", "E2"])
        [chat] = self.fake.chats("scout")
        self.assertEqual(chat["model"], MODEL)
        self.assertEqual(chat["options"]["num_ctx"], 8192)
        self.assertEqual(chat["format"]["required"], ["candidates", "nothing_because"])
        self.assertFalse(chat["stream"])
        self.assertTrue(list((self.repo / ".review" / "local_team" / "candidates").glob("*.json")))

        # Nothing new: no model call at all.
        before = len(self.fake.chats())
        again = roles.scout(self.ctx)
        self.assertEqual(again["evidence"], [])
        self.assertEqual(len(self.fake.chats()), before)

        # A new log line is the only new evidence next time.
        with (self.repo / ".review" / "runner.log").open("a", encoding="utf-8") as f:
            f.write("2026-11-02T20:00:00 FAIL 2026-11-02T1955-c after 60s\n")
        self.fake.answers["scout"] = [{"candidates": [], "nothing_because": "one fail, no pattern"}]
        third = roles.scout(self.ctx)
        self.assertEqual([e["text"] for e in third["evidence"]], ["2026-11-02T20:00:00 FAIL 2026-11-02T1955-c after 60s"])

    def test_scout_cursor_stays_put_when_skipped(self):
        lease = GpuLease.for_repo(self.repo, "denisse eval")
        self.assertTrue(lease.try_acquire())
        try:
            with self.assertRaises(core.Skip):
                roles.scout(self.ctx)
        finally:
            lease.release()
        self.assertNotIn("scout", self.ctx.state())
        self.assertEqual(self.fake.chats(), [])

    # ------------------------------------------------------------ planner

    def candidate(self):
        self.fake.answers["scout"] = [self.scout_answer(extra_bad=False, ids=("E2",))]
        return roles.scout(self.ctx)["candidates"][0]

    def test_planner_rejects_answers_outside_its_schema(self):
        c = self.candidate()
        for bad in ("not json at all", {"title": "x"}, self.planner_answer(weight="huge"),
                    self.planner_answer(gate={"id": "unittest"}), {**self.planner_answer(), "shell": "rm -rf ."}):
            with self.subTest(bad=bad):
                self.fake.answers["planner"] = [bad]
                with self.assertRaises(core.BadOutput):
                    roles.plan(self.ctx, c, admit_it=True, lane="claude")
        self.assertEqual(list((self.repo / ".review" / "queue").glob("*.md")), [])

    def test_planner_packet_goes_through_admission_as_draft(self):
        c = self.candidate()
        self.fake.answers["planner"] = [self.planner_answer()]
        res = roles.plan(self.ctx, c, admit_it=True, lane="claude")
        self.assertTrue(res["admission"]["admitted"], res["admission"])
        self.assertEqual(res["packet"]["evidence"][0]["path"], ".review/runner.log")
        self.assertEqual(res["packet"]["roles"], {"scout": "1", "planner": "1"})
        [task] = (self.repo / ".review" / "queue").glob("*.md")
        text = task.read_text("utf-8")
        self.assertIn("Queue: draft", text)
        self.assertIn(".venv\\Scripts\\python.exe -m unittest orchestra.test_runner -v", text)
        payload = json.loads(self.fake.chats("planner")[0]["messages"][1]["content"])
        self.assertIn("unittest", payload["gates"])
        self.assertIn("orchestra/test_runner.py", payload["test_files"])

    def test_planner_cannot_invent_a_gate_or_escape_the_repo(self):
        c = self.candidate()
        self.fake.answers["planner"] = [self.planner_answer(gate={"id": "powershell", "arg": "Remove-Item C:\\"})]
        self.assertEqual(roles.plan(self.ctx, c, admit_it=True, lane="claude")["admission"]["kind"], "gate")
        c2 = dict(c, evidence_items=[dict(c["evidence_items"][0])])
        self.fake.answers["planner"] = [self.planner_answer(title="Write a note into the vault folder now",
                                                            scope_paths=["../../Users/x/note.md"])]
        self.assertEqual(roles.plan(self.ctx, c2, admit_it=True, lane="claude")["admission"]["kind"], "path")
        self.assertEqual(list((self.repo / ".review" / "queue").glob("*.md")), [])

    # ------------------------------------------------------------ librarian

    def test_librarian_writes_data_only_with_provenance(self):
        rep = self.repo / ".review" / "archive" / "2026-11-02" / "2026-11-02T1800-reconcile.report.md"
        self.fake.answers["librarian"] = [{"lines": [
            {"text": "A hung orphan lane keeps its runner slot until it exits; nothing kills it.", "section": "NOT DONE"},
            {"text": "Use api_key: sk-123 for the hub", "section": "ODD"}]}]
        out = roles.librarian(self.ctx, rep)
        self.assertEqual(len(out["lines"]), 1)  # the credential-looking line is dropped
        line = out["lines"][0]["text"]
        self.assertTrue(line.startswith("FOR MEMORY: A hung orphan lane"))
        self.assertIn("source: .review/archive/2026-11-02/2026-11-02T1800-reconcile.report.md", line)
        self.assertIn("RESULT PARTIAL", line)
        self.assertTrue((self.repo / ".review" / "local_team" / "memory" / "2026-11-02T1800-reconcile.json").exists())
        self.assertEqual(list((self.repo / "vault").iterdir()), [])
        self.fake.answers["librarian"] = [{"lines": [{"text": f"fact number {i} here", "section": "DONE"}
                                                     for i in range(4)]}]
        with self.assertRaises(core.BadOutput):  # more than 3
            roles.librarian(self.ctx, rep)

    # ------------------------------------------------------------ producer and skips

    def test_produce_end_to_end(self):
        self.fake.answers["librarian"] = [{"lines": [{"text": "Orphan lanes are collected by reconcile() in runner.py.",
                                                      "section": "DONE"}]}]
        self.fake.answers["scout"] = [self.scout_answer(ids=("E2",))]
        self.fake.answers["planner"] = [self.planner_answer()]
        rc = cli.main(["produce", "--repo", str(self.repo), "--lane", "claude"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(list((self.repo / ".review" / "queue").glob("*.md"))), 1)
        self.assertEqual(len(list((self.repo / ".review" / "local_team" / "memory").glob("*.json"))), 1)
        self.assertFalse((self.repo / ".review" / "gpu.lease").exists())  # released
        self.assertFalse(any(p != "/api/chat" and m == "POST" for m, p, _ in self.fake.requests))  # no pull
        # Second run: the report is read, no new evidence -> no model call.
        before = len(self.fake.chats())
        self.assertEqual(cli.main(["produce", "--repo", str(self.repo), "--lane", "claude"]), 0)
        self.assertEqual(len(self.fake.chats()), before)

    def test_produce_does_not_scan_when_queue_has_enough(self):
        for i in range(6):
            (self.repo / ".review" / "queue" / f"t{i}.md").write_text("# t\nQueue: auto\n", encoding="utf-8")
        (self.repo / ".review" / "archive" / "2026-11-02" / "2026-11-02T1800-reconcile.report.md").unlink()
        roles.produce(self.ctx)
        self.assertEqual(self.fake.chats(), [])

    def test_skips_cleanly_when_ollama_is_down_or_model_missing_or_gpu_busy(self):
        down = core.Ctx(self.repo, ollama="http://127.0.0.1:9")
        with self.assertRaises(core.Skip):
            core.ready(down)
        self.fake.models = ["qwen2.5:7b"]
        with self.assertRaisesRegex(core.Skip, "not pulling"):
            core.ready(self.ctx)
        self.fake.models = [MODEL]
        self.fake.loaded = ["qwen2.5:7b"]
        with self.assertRaisesRegex(core.Skip, "evict"):
            core.ready(self.ctx)
        self.fake.loaded = [MODEL]
        self.assertEqual(core.ready(self.ctx), MODEL)
        with mock.patch.object(core, "gpu_utilisation", return_value=95):
            with self.assertRaisesRegex(core.Skip, "GPU 95% busy"):
                core.ready(self.ctx)
        # Right after our own call nvidia-smi still shows it: re-sample once, don't skip ourselves.
        with mock.patch.object(core, "_last_call_end", core.time.monotonic()), \
                mock.patch.object(core.time, "sleep") as slept:
            with mock.patch.object(core, "gpu_utilisation", side_effect=[97, 5]):
                self.assertEqual(core.ready(self.ctx), MODEL)
            with mock.patch.object(core, "gpu_utilisation", side_effect=[97, 90]):
                with self.assertRaisesRegex(core.Skip, "GPU 90% busy"):
                    core.ready(self.ctx)
            self.assertEqual(slept.call_count, 2)
        with mock.patch.dict(os.environ, {"OLLAMA_HOST_URL": "http://127.0.0.1:9"}):
            self.assertEqual(cli.main(["scout", "--repo", str(self.repo)]), 0)
        self.assertIn("scout: skipped", (self.repo / ".review" / "local_team" / "producer.log").read_text("utf-8"))
        self.assertEqual(self.fake.chats(), [])


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / ".review" / "gpu.lease"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_holder_at_a_time(self):
        a, b = GpuLease(self.path, "a"), GpuLease(self.path, "b")
        self.assertTrue(a.try_acquire())
        self.assertFalse(b.try_acquire())
        self.assertEqual(b.info()["holder"], "a")
        a.release()
        self.assertTrue(b.try_acquire())
        b.release()
        self.assertFalse(self.path.exists())

    def test_dead_holder_or_expired_lease_is_taken_over(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"pid": 2_000_000_000, "holder": "gone", "since": "2026-11-02T20:00:00",
                                         "ttl_s": 900}), encoding="utf-8")
        lease = GpuLease(self.path, "me")
        self.assertIsNone(lease.info())
        self.assertTrue(lease.try_acquire())
        lease.release()
        self.path.write_text(json.dumps({"pid": os.getpid(), "holder": "old", "since": "2020-01-01T00:00:00",
                                         "ttl_s": 900}), encoding="utf-8")
        self.assertTrue(GpuLease(self.path, "me").try_acquire())

    def test_release_never_deletes_someone_elses_lease(self):
        a = GpuLease(self.path, "a")
        self.assertTrue(a.try_acquire())
        data = json.loads(self.path.read_text("utf-8"))
        data["pid"] = data["pid"] + 1_000_000  # someone took it over meanwhile
        self.path.write_text(json.dumps(data), encoding="utf-8")
        a.release()
        self.assertTrue(self.path.exists())

    def test_hold_waits_then_gives_up(self):
        a = GpuLease(self.path, "a")
        self.assertTrue(a.try_acquire())
        with GpuLease(self.path, "b").hold(wait_s=0.3, poll_s=0.1) as got:
            self.assertFalse(got)
        a.release()
        with GpuLease(self.path, "b").hold() as got:
            self.assertTrue(got)
        self.assertFalse(self.path.exists())

    def test_worktree_uses_the_main_checkouts_lease(self):
        main = self.tmp / "denisse"
        (main / ".git" / "worktrees" / "wt1").mkdir(parents=True)
        wt = self.tmp / "denisse-wt" / "wt1"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt1'}\n", encoding="utf-8")
        self.assertEqual(main_checkout(wt).resolve(), main.resolve())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORCHESTRA_GPU_LEASE", None)
            self.assertEqual(GpuLease.for_repo(wt, "x").path.resolve(), (main / ".review" / "gpu.lease").resolve())


if __name__ == "__main__":
    unittest.main()
