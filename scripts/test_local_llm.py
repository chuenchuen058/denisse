"""local_llm.py: the one path every local model call takes (10 Nov 2026 Ollama wedge).

Fakes throughout: no Ollama, no real lease file, no process is killed or started.

    .venv\\Scripts\\python.exe -m unittest scripts.test_local_llm -v
"""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import local_llm  # noqa: E402


def cfg(**over):
    c = local_llm.config()
    for k, v in over.items():
        c[k] = {**c[k], **v} if isinstance(v, dict) and isinstance(c.get(k), dict) else v
    return c


class FakeLease:
    """GpuLease's four methods, over a dict instead of a file."""

    def __init__(self, held_by=None, frees=True):
        self.held_by, self.frees = held_by, frees
        self.acquired = self.released = 0
        self.waits = []

    def info(self):
        return self.held_by

    def acquire(self, wait_s=0, poll_s=None):
        self.waits.append(wait_s)
        if self.held_by is not None and not self.frees:
            return False
        self.held_by = {"pid": -1, "holder": "us"}
        self.acquired += 1
        return True

    def release(self):
        self.released += 1
        self.held_by = None


def opener_for(ps=None, error=None):
    """A urlopen stand-in for /api/ps."""
    def opener(url, timeout=None):
        if error is not None:
            raise error
        m = mock.MagicMock()
        m.__enter__.return_value = io.BytesIO(json.dumps(ps or {"models": []}).encode())
        return m
    return opener


def ago(seconds):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class OneContext(unittest.TestCase):
    def test_a_callers_num_ctx_is_replaced_by_the_models_one_value(self):
        c = cfg(num_ctx_by_model={"tiny:1b": 2048})
        with mock.patch.dict(os.environ, {c["context"]["env"]: "8192"}):
            self.assertEqual(local_llm.options("big:8b", {"num_ctx": 4096}, cfg=c)["num_ctx"], 8192)
            self.assertEqual(local_llm.options("big:8b", None, cfg=c)["num_ctx"], 8192)
            self.assertEqual(local_llm.options("tiny:1b", {"num_ctx": 8192}, cfg=c)["num_ctx"], 2048)

    def test_every_reply_is_capped(self):
        c = cfg(num_predict=100, num_predict_max=300)
        self.assertEqual(local_llm.options("m", {}, cfg=c)["num_predict"], 100)
        self.assertEqual(local_llm.options("m", {"num_predict": 50}, cfg=c)["num_predict"], 50)
        self.assertEqual(local_llm.options("m", {"num_predict": 10_000}, cfg=c)["num_predict"], 300)
        self.assertEqual(local_llm.options("m", {"num_predict": -1}, cfg=c)["num_predict"], 100)

    def test_embedding_options_are_left_alone(self):
        self.assertEqual(local_llm.options("embedder", {"x": 1}, kind="embed"), {"x": 1})

    def test_a_prompt_too_long_for_the_context_is_flagged(self):
        c = cfg(chars_per_token=2.5)
        opts = {"num_ctx": 1000, "num_predict": 200}
        self.assertIsNone(local_llm.too_long({"messages": [{"content": "x" * 1000}]}, opts, c))
        self.assertIn("> 800", local_llm.too_long({"messages": [{"content": "x" * 5000}]}, opts, c))

    def test_fakes_still_see_the_one_num_ctx(self):
        import ollama
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return {"message": {"content": "ok"}}

        with mock.patch.object(ollama, "chat", fake):
            local_llm.chat(job="t", model="m", messages=[], options={"num_ctx": 4096})
        self.assertEqual(seen["options"]["num_ctx"], local_llm.num_ctx("m"))
        self.assertIn("num_predict", seen["options"])


class Timeouts(unittest.TestCase):
    def test_by_job_then_kind_then_default_and_never_longer_than_asked(self):
        c = cfg(timeout_s=300, timeout_s_by_kind={"embed": 60}, timeout_s_by_job={"slow": 900})
        self.assertEqual(local_llm.timeout_for("chat", None, cfg=c), 300)
        self.assertEqual(local_llm.timeout_for("embed", None, cfg=c), 60)
        self.assertEqual(local_llm.timeout_for("chat", "slow", cfg=c), 900)
        self.assertEqual(local_llm.timeout_for("chat", None, asked=30, cfg=c), 30)

    def test_the_real_client_gets_the_timeout_and_the_guarded_path(self):
        import ollama
        made, guarded = [], []

        class Client:
            def __init__(self, host=None, timeout=None):
                made.append(timeout)

            def chat(self, **kw):
                return {"message": {"content": "ok"}}

        def fake_guarded(job, kind, t, send, **kw):
            guarded.append((job, kind, t))
            return send()

        with mock.patch.object(local_llm, "_real_ollama", return_value=True), \
                mock.patch.object(ollama, "Client", Client), \
                mock.patch.object(sys.modules["ollama._client"], "Client", Client), \
                mock.patch.object(local_llm, "_guarded", fake_guarded):
            local_llm.chat(job="don_extract", model="m", messages=[], timeout=12)
        self.assertEqual(made, [12.0])
        self.assertEqual(guarded, [("don_extract", "chat", 12.0)])


class Lease(unittest.TestCase):
    def test_a_free_lease_is_taken_and_given_back(self):
        fake = FakeLease()
        with local_llm.lease("j", 10, factory=lambda h, t: fake) as got:
            self.assertIs(got, True)
        self.assertEqual((fake.acquired, fake.released), (1, 1))

    def test_held_by_this_process_or_the_named_parent_is_reentrant(self):
        mine = FakeLease(held_by={"pid": os.getpid()})
        with local_llm.lease("j", 10, factory=lambda h, t: mine) as got:
            self.assertEqual(got, "held")
        self.assertEqual((mine.acquired, mine.released), (0, 0))
        parent = FakeLease(held_by={"pid": 424242})
        with mock.patch.dict(os.environ, local_llm.owner_env(424242)):
            with local_llm.lease("j", 10, factory=lambda h, t: parent) as got:
                self.assertEqual(got, "held")
        self.assertEqual(parent.released, 0)

    def test_busy_past_the_wait_raises_or_runs_by_job(self):
        c = cfg(lease={"wait_s": 3, "on_busy": "raise", "by_job": {"live": {"wait_s": 1, "on_busy": "run"}}})
        busy = FakeLease(held_by={"pid": 999999, "holder": "eval"}, frees=False)
        with self.assertRaises(local_llm.LeaseBusy) as e:
            with local_llm.lease("batch", 10, factory=lambda h, t: busy, cfg=c):
                pass
        self.assertIsInstance(e.exception, TimeoutError)
        self.assertIn("eval", str(e.exception))
        with local_llm.lease("live", 10, factory=lambda h, t: busy, cfg=c) as got:
            self.assertIs(got, False)
        self.assertEqual(busy.waits, [3, 1])

    def test_the_ttl_covers_the_call(self):
        seen = []
        c = cfg(lease={"ttl_margin_s": 30})
        with local_llm.lease("j", 100, factory=lambda h, t: seen.append(t) or FakeLease(), cfg=c):
            pass
        self.assertEqual(seen, [130])

    def test_a_real_http_call_runs_under_the_lease(self):
        fake, order = FakeLease(), []

        def urlopen(req, timeout=None):
            order.append(("call" if hasattr(req, "data") else "ps", fake.held_by is not None, timeout))
            m = mock.MagicMock()
            m.__enter__.return_value = io.BytesIO(b'{"models": []}' if not hasattr(req, "data")
                                                  else b'{"message": {"content": "{}"}}')
            return m

        urlopen.__module__, urlopen.__name__ = "urllib.request", "urlopen"
        data = local_llm.http("http://x", "/api/chat", {"model": "m", "messages": []}, job="j", timeout=7,
                              opener=urlopen, lease_factory=lambda h, t: fake)
        self.assertEqual(data["message"]["content"], "{}")
        self.assertEqual([o[:2] for o in order], [("ps", True), ("call", True)])
        self.assertEqual(order[1][2], 7.0)
        self.assertEqual(fake.released, 1)


class Wedge(unittest.TestCase):
    def test_stopping_too_long_is_a_wedge_and_recent_or_future_is_not(self):
        c = cfg(wedge={"stopping_s": 60})
        self.assertIn("Stopping for", local_llm.wedged({"models": [{"name": "m", "expires_at": ago(600)}]}, cfg=c))
        self.assertIsNone(local_llm.wedged({"models": [{"name": "m", "expires_at": ago(5)}]}, cfg=c))
        self.assertIsNone(local_llm.wedged({"models": [{"name": "m", "expires_at": ago(-600)}]}, cfg=c))
        self.assertIsNone(local_llm.wedged({"models": []}, cfg=c))

    def test_ollamas_nanosecond_timestamps_parse(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
        self.assertIsNotNone(local_llm.wedged({"models": [{"name": "m", "expires_at": old}]}))

    def test_a_hanging_ps_is_a_wedge_and_a_refused_one_is_not(self):
        self.assertIsNotNone(local_llm.wedged(None, ps_error=TimeoutError("timed out")))
        import urllib.error
        self.assertIsNotNone(local_llm.wedged(None, ps_error=urllib.error.URLError(TimeoutError())))
        self.assertIsNone(local_llm.wedged(None, ps_error=ConnectionRefusedError()))

    def test_the_restart_decision(self):
        c = cfg(wedge={"auto_restart": True, "min_restart_interval_s": 600})
        self.assertTrue(local_llm.may_restart({}, 1000.0, c))
        self.assertFalse(local_llm.may_restart({"last_restart": 900.0}, 1000.0, c))
        self.assertTrue(local_llm.may_restart({"last_restart": 100.0}, 1000.0, c))
        self.assertFalse(local_llm.may_restart({}, 1000.0, cfg(wedge={"auto_restart": False})))


class Restart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.ran, self.started = [], []
        self.c = cfg(wedge={"auto_restart": True, "min_restart_interval_s": 600, "up_wait_s": 4, "up_poll_s": 1,
                            "restart": {"testos": {"kill": [["kill", "a"], ["kill", "b"]], "start": ["start-it"]}}})

    def go(self, opener, clock=None):
        t = iter(range(0, 1000))
        return local_llm.restart("why", runner=lambda argv, **k: self.ran.append(argv),
                                 popen=lambda argv, **k: self.started.append(argv), opener=opener,
                                 clock=clock or (lambda: float(next(t))), sleep=lambda s: None,
                                 state_path=self.dir / "state.json", log_path=self.dir / "log", cfg=self.c,
                                 platform="testos")

    def test_kills_starts_waits_and_logs(self):
        self.assertTrue(self.go(opener_for()))
        self.assertEqual(self.ran, [["kill", "a"], ["kill", "b"]])
        self.assertEqual(self.started, [["start-it"]])
        log = (self.dir / "log").read_text(encoding="utf-8")
        self.assertIn("WEDGED (why); restarting Ollama", log)
        self.assertIn("answers again", log)

    def test_rate_limited(self):
        self.assertTrue(self.go(opener_for(), clock=lambda: 1000.0))
        self.ran.clear()
        self.assertFalse(self.go(opener_for(), clock=lambda: 1100.0))
        self.assertEqual(self.ran, [])
        self.assertIn("not restarting", (self.dir / "log").read_text(encoding="utf-8"))

    def test_no_commands_for_the_platform_only_logs(self):
        self.c["wedge"]["restart"] = {}
        self.assertFalse(self.go(opener_for()))
        self.assertEqual((self.ran, self.started), ([], []))
        self.assertIn("by hand", (self.dir / "log").read_text(encoding="utf-8"))

    def test_still_down_after_the_wait_is_logged(self):
        self.assertFalse(self.go(opener_for(error=ConnectionRefusedError())))
        self.assertIn("still not answering", (self.dir / "log").read_text(encoding="utf-8"))

    def test_a_timed_out_call_checks_for_a_wedge_and_restarts(self):
        restarted = []
        wedged_ps = opener_for({"models": [{"name": "m", "expires_at": ago(3600)}]})
        calls = iter([opener_for(), wedged_ps])  # free before the call, wedged after it

        def opener(url, timeout=None):
            return next(calls)(url, timeout)

        def send():
            raise TimeoutError("timed out")

        with mock.patch.object(local_llm, "restart", lambda why, **k: restarted.append(why)):
            with self.assertRaises(TimeoutError):
                local_llm._guarded("j", "chat", 5, send, base="http://x", opener=opener,
                                   lease_factory=lambda h, t: FakeLease(), cfg=self.c)
        self.assertEqual(len(restarted), 1)
        self.assertIn("Stopping", restarted[0])


if __name__ == "__main__":
    unittest.main()
