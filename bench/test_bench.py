"""The bench with a fake model: no Ollama, no GPU, no network, no vault.

    .venv\\Scripts\\python.exe -m unittest bench.test_bench -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench import core, jobs, meter, score  # noqa: E402
import model_routing  # noqa: E402

CFG = core.load_config()


def _fixture(name):
    return json.loads((ROOT / CFG["jobs"][name]["fixtures"]).read_text(encoding="utf-8"))["cases"]


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.])\s+|\n+", text) if len(s.strip()) > 20]


def _case_for(user, name, key="text"):
    """The fixture case whose source the prompt carries."""
    for c in _fixture(name):
        if c[key][:60] in user:
            return c
    raise AssertionError(f"no {name} fixture in prompt")


def good_call(system, user, schema=None, options=None):
    """A model that answers every Don prompt from the source it was given, as JSON."""
    keys = set(json.loads(schema)) if schema else set()
    if "outcomes" in keys:
        c = _case_for(user, "don_extract")
        s = _sentences(c["text"])
        return json.dumps({"outcomes": s, "methods": [{"name": s[1], "equation": "", "use": s[2]}],
                           "examples": [], "check": [], "topics": [{"name": s[0], "prerequisites": []}]})
    if "cards" in keys:
        c = _case_for(user, "don_cards")
        s = _sentences(c["text"])
        return json.dumps({"cards": [{"kind": "recall", "front": f"What does the note say ({i})?", "back": x,
                                      "topic": c["topic"], "quote": x} for i, x in enumerate(s[:4], 1)]})
    if "checks" in keys:
        c = _case_for(user, "don_card_check")
        order = sorted((user.index(x["back"]), x) for x in c["cards"] if x["back"] in user)
        return json.dumps({"checks": [{"n": n, "verdict": x["truth"], "reason": "per the note"}
                                      for n, (_pos, x) in enumerate(order, 1)]})
    if "steps" in keys:
        c = _case_for(user, "don_teach")
        s = _sentences(c["text"])
        return json.dumps({"summary": s[0], "steps": s[1:], "question": s[0], "answer": s[-1], "confident": True})
    if "caveats" in keys:
        c = _case_for(user, "don_consult")
        return json.dumps({"answer": c["text"], "caveats": [], "confident": True})
    raise AssertionError(f"unexpected prompt shape {keys}")


def bad_call(system, user, schema=None, options=None):
    return "I am not sure."


def good_classify(cand, cfg):
    c = next(x for x in _fixture("atrium_autopilot") if x["text"] == cand.text)
    out = {k: type(v)() for k, v in cfg["autopilot_classify_example"].items()}
    out.update(c["expect"])
    return out


def bad_classify(cand, cfg):
    raise ValueError("model reply: actionable is not a bool")


def fake_eval(passed):
    def run(model, spec):
        rows = [{"id": i, "passed": passed, "seconds": 2.0, "robotic": [], "failures": [] if passed else ["x"],
                 "reply": "SECRET REPLY", "tools": []} for i in spec["only"]]
        return {"clean": rows, "live": rows, "timing": {"gen_tokens": 300, "gen_s": 3.0, "calls": 4}}
    return run


def fake_local_team(right):
    def run(model, spec):
        exp = {f["name"]: f["expected"]["candidate"] for f in __import__(
            "orchestra.local_team.baseline", fromlist=["x"]).load_fixtures(only=spec["only"])}
        runs = [{"fixture": n, "round": 1, "seconds": 5.0, "plans": [],
                 "candidates": [{"problem": "p"}] if (want if right else not want) else []} for n, want in exp.items()]
        return {"runs": runs, "score": {"fixture_accuracy_pct": 100 if right else 0}}
    return run


def env_for(good: bool):
    def make(model):
        return jobs.Env(model=model, tutor_call=good_call if good else bad_call,
                        classify=good_classify if good else bad_classify,
                        eval_run=fake_eval(good), local_team_run=fake_local_team(good))
    return make


class FakeLease:
    def __init__(self, free=True):
        self.free, self.held, self.acquired = free, False, 0

    def acquire(self, wait_s=0, poll_s=None):
        if self.free:
            self.held = True
            self.acquired += 1
        return self.free

    def release(self):
        self.held = False

    def info(self):
        return None if self.free else {"holder": "eval/run_eval.py"}


AC = {"ac": True, "battery_percent": None}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bench-test-"))
        self.cfg = json.loads(json.dumps(CFG))
        self.cfg["out_dir"] = str(self.tmp / "out")
        self.lines = []

    def run_bench(self, models, names=None, good=None, lease=None, power=None, installed=None, **kw):
        good = good or {}
        return core.run(models, names or core.job_names(self.cfg, "all"), cfg=self.cfg, date="2026-11-10",
                        root=ROOT, env_factory=lambda m: env_for(good.get(m, True))(m),
                        lease=lease or FakeLease(), power=power or (lambda: AC),
                        installed=installed or (lambda: set(models)), vram=lambda m: {"vram_mib": 5000, "size_mib": 6000},
                        log=self.lines.append, **kw)

    def results(self):
        return core.load_results(core.results_path(self.cfg, "2026-11-10", ROOT))


class TestRun(Base):
    def test_every_job_scores_a_good_fake_model_full_marks(self):
        code = self.run_bench(["good:1b"])
        self.assertEqual(code, 0, self.lines)
        data = self.results()
        for job in core.job_names(self.cfg, "all"):
            cell = data["cells"][core.cell_key("good:1b", job)]
            self.assertEqual(cell["status"], "done", (job, cell))
            failing = [(c["id"], {k: v for k, v in c["checks"].items() if not v}, c["error"])
                       for c in cell["cases"] if not c["ok"]]
            self.assertEqual(cell["summary"]["score"], 1.0, (job, failing))
            self.assertEqual(cell["metrics"]["vram_mib"], 5000)

    def test_a_bad_fake_model_scores_low_and_is_not_an_error(self):
        self.run_bench(["bad:1b"], good={"bad:1b": False})
        data = self.results()
        for job in core.job_names(self.cfg, "all"):
            cell = data["cells"][core.cell_key("bad:1b", job)]
            self.assertEqual(cell["status"], "done", job)
            self.assertLess(cell["summary"]["score"], 1.0, job)

    def test_writes_json_and_a_markdown_table(self):
        self.run_bench(["good:1b", "bad:1b"], ["don_consult"], good={"bad:1b": False})
        md = core.results_path(self.cfg, "2026-11-10", ROOT).with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("| job | good:1b | bad:1b |", md)
        self.assertIn("| don_consult | **1.0**", md)

    def test_eval_replies_are_not_kept(self):
        self.run_bench(["good:1b"], ["denisse_brain"])
        raw = core.results_path(self.cfg, "2026-11-10", ROOT).read_text(encoding="utf-8")
        self.assertNotIn("SECRET REPLY", raw)
        cell = self.results()["cells"][core.cell_key("good:1b", "denisse_brain")]
        self.assertEqual(cell["metrics"]["tokens_per_s"], 100.0)
        self.assertEqual(cell["summary"]["cases"], 2 * len(self.cfg["jobs"]["denisse_brain"]["only"]))

    def test_resumes_cells_already_done(self):
        self.run_bench(["good:1b"], ["don_consult", "don_teach"])
        calls = []

        def counting(model):
            e = env_for(True)(model)
            real = e.tutor_call
            e.tutor_call = lambda *a, **k: calls.append(1) or real(*a, **k)
            return e
        core.run(["good:1b"], ["don_consult", "don_teach"], cfg=self.cfg, date="2026-11-10", root=ROOT,
                 env_factory=counting, lease=FakeLease(), power=lambda: AC, installed=lambda: {"good:1b"},
                 vram=lambda m: {}, log=self.lines.append)
        self.assertEqual(calls, [])
        self.assertTrue(any("done earlier" in line for line in self.lines))

    def test_fresh_starts_over(self):
        self.run_bench(["good:1b"], ["don_consult"])
        self.run_bench(["good:1b"], ["don_consult"], fresh=True)
        self.assertFalse(any("done earlier" in line for line in self.lines))

    def test_stops_on_battery_and_resumes_on_ac(self):
        reads = iter([AC, {"ac": False, "battery_percent": 80}])
        code = self.run_bench(["good:1b"], ["don_consult", "don_teach"], power=lambda: next(reads))
        self.assertEqual(code, core.EXIT["battery"])
        data = self.results()
        self.assertEqual(data["cells"][core.cell_key("good:1b", "don_consult")]["status"], "done")
        self.assertNotIn(core.cell_key("good:1b", "don_teach"), data["cells"])
        self.assertIn("battery", data["stopped"])
        self.assertEqual(self.run_bench(["good:1b"], ["don_consult", "don_teach"]), 0)
        self.assertNotIn("stopped", self.results())

    def test_unknown_power_counts_as_ac(self):
        self.assertEqual(self.run_bench(["good:1b"], ["don_consult"],
                                        power=lambda: {"ac": None, "battery_percent": None}), 0)

    def test_busy_lease_stops_before_an_in_process_job(self):
        lease = FakeLease(free=False)
        code = self.run_bench(["good:1b"], ["don_consult"], lease=lease)
        self.assertEqual(code, core.EXIT["lease"])
        self.assertIn("eval/run_eval.py", self.results()["stopped"])

    def test_own_lease_jobs_run_with_the_bench_hold_released(self):
        lease = FakeLease()
        seen = []

        def make(model):
            e = env_for(True)(model)
            e.eval_run = lambda m, spec: seen.append(lease.held) or fake_eval(True)(m, spec)
            return e
        core.run(["good:1b"], ["don_consult", "denisse_brain"], cfg=self.cfg, date="2026-11-10", root=ROOT,
                 env_factory=make, lease=lease, power=lambda: AC, installed=lambda: {"good:1b"},
                 vram=lambda m: {}, log=self.lines.append)
        self.assertEqual(seen, [False])
        self.assertFalse(lease.held)

    def test_never_pulls_a_missing_model(self):
        code = self.run_bench(["missing:7b"], ["don_consult"], installed=lambda: {"other:1b"})
        self.assertEqual(code, core.EXIT["incomplete"])
        cell = self.results()["cells"][core.cell_key("missing:7b", "don_consult")]
        self.assertEqual(cell["status"], "skipped")

    def test_a_skipped_job_is_retried(self):
        def make(model):
            e = env_for(True)(model)

            def busy(m, spec):
                raise jobs.Skipped("GPU lease busy")
            e.local_team_run = busy
            return e
        core.run(["good:1b"], ["orchestra_local_team"], cfg=self.cfg, date="2026-11-10", root=ROOT,
                 env_factory=make, lease=FakeLease(), power=lambda: AC, installed=lambda: {"good:1b"},
                 vram=lambda m: {}, log=self.lines.append)
        self.assertEqual(self.results()["cells"][core.cell_key("good:1b", "orchestra_local_team")]["status"], "skipped")
        self.run_bench(["good:1b"], ["orchestra_local_team"])
        self.assertEqual(self.results()["cells"][core.cell_key("good:1b", "orchestra_local_team")]["status"], "done")

    def test_routing_override_is_set_only_during_a_cell(self):
        env_name = model_routing.override_env()
        seen = []

        def make(model):
            e = env_for(True)(model)
            real = e.tutor_call
            e.tutor_call = lambda *a, **k: seen.append(os.environ.get(env_name)) or real(*a, **k)
            return e
        before = os.environ.get(env_name)
        core.run(["good:1b"], ["don_consult"], cfg=self.cfg, date="2026-11-10", root=ROOT, env_factory=make,
                 lease=FakeLease(), power=lambda: AC, installed=lambda: {"good:1b"}, vram=lambda m: {},
                 log=self.lines.append)
        self.assertEqual(set(seen), {"good:1b"})
        self.assertEqual(os.environ.get(env_name), before)


class TestPick(Base):
    def test_picks_the_better_model_and_never_edits_routing(self):
        before = model_routing.path().read_text(encoding="utf-8")
        self.run_bench(["good:1b", "bad:1b"], ["don_consult", "atrium_autopilot"], good={"bad:1b": False})
        rows = {r["job"]: r for r in core.pick(self.results(), self.cfg, ROOT)}
        self.assertEqual(rows["don_consult"]["best"], "good:1b")
        self.assertEqual(rows["atrium_autopilot"]["best"], "good:1b")
        self.assertTrue(rows["don_consult"]["change"])
        text = "\n".join(core.pick_lines(list(rows.values()), self.cfg, Path("x.json")))
        self.assertIn('"don_consult": "good:1b"', text)
        self.assertEqual(model_routing.path().read_text(encoding="utf-8"), before)

    def test_a_tie_goes_to_the_faster_model(self):
        data = {"jobs": ["don_consult"], "models": ["slow", "fast"], "cells": {
            core.cell_key("slow", "don_consult"): {"status": "done", "summary": {
                "score": 1.0, "ok": 2, "cases": 2, "errors": 0, "median_s": 9.0}},
            core.cell_key("fast", "don_consult"): {"status": "done", "summary": {
                "score": 0.98, "ok": 2, "cases": 2, "errors": 0, "median_s": 2.0}}}}
        self.assertEqual(core.pick(data, self.cfg, ROOT)[0]["best"], "fast")

    def test_keeps_today_without_results(self):
        row = core.pick({"jobs": ["don_teach"], "models": ["x"], "cells": {}}, self.cfg, ROOT)[0]
        self.assertIsNone(row["best"])
        self.assertFalse(row["change"])

    def test_latest_results_file(self):
        out = core.out_dir(self.cfg, ROOT)
        out.mkdir(parents=True)
        for d in ("2026-11-08", "2026-11-10", "2026-11-09"):
            (out / f"{d}.json").write_text("{}", encoding="utf-8")
        self.assertEqual(core.latest(self.cfg, ROOT).name, "2026-11-10.json")


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="routing-"))
        self.file = self.tmp / "routing.json"
        self.file.write_text(json.dumps({"override_env": "BENCH_TEST_OVERRIDE", "jobs": {
            "pinned": {"model": "pinned:1b"}, "free": {"model": None}}}), encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"MODEL_ROUTING": str(self.file)})
        self.env.start()
        os.environ.pop("BENCH_TEST_OVERRIDE", None)
        os.environ.pop("DENISSE_MODEL", None)
        self.addCleanup(self.env.stop)

    def test_null_keeps_today(self):
        self.assertEqual(model_routing.model_for("free", "today:1b"), "today:1b")
        self.assertEqual(model_routing.model_for("unknown", "today:1b"), "today:1b")

    def test_pinned_wins_over_today(self):
        self.assertEqual(model_routing.model_for("pinned", "today:1b"), "pinned:1b")

    def test_override_wins_over_everything(self):
        os.environ["BENCH_TEST_OVERRIDE"] = "bench:1b"
        self.assertEqual(model_routing.model_for("pinned", "today:1b"), "bench:1b")
        self.assertEqual(model_routing.model_for("pinned", entry="chat"), "bench:1b")

    def test_entry_env_still_wins_over_routing(self):
        import model_config
        self.assertEqual(model_routing.model_for("free", entry="chat"), model_config.entry("chat")["value"])
        self.assertEqual(model_routing.model_for("pinned", entry="chat"), "pinned:1b")
        os.environ["DENISSE_MODEL"] = "env:1b"
        self.assertEqual(model_routing.model_for("pinned", entry="chat"), "env:1b")

    def test_missing_file_keeps_today(self):
        os.environ["MODEL_ROUTING"] = str(self.tmp / "nope.json")
        self.assertEqual(model_routing.model_for("pinned", "today:1b"), "today:1b")

    def test_don_operations_are_routed_by_job(self):
        from tutor import model
        jobs_ = model._M["route_jobs"]
        self.file.write_text(json.dumps({"override_env": "BENCH_TEST_OVERRIDE", "jobs": {
            jobs_["cards"]: {"model": "cards:1b"}}}), encoding="utf-8")
        import model_config
        self.assertEqual(model.model_name(), model_config.entry("chat")["value"])
        with model.job("cards"):
            self.assertEqual(model.model_name(), "cards:1b")
            with model.job("teach"):
                self.assertEqual(model.model_name(), model_config.entry("chat")["value"])
        self.assertEqual(model.model_name(), model_config.entry("chat")["value"])

    def test_the_call_reaches_ollama_with_the_routed_model(self):
        from tutor import model
        seen = {}

        def chat(**kw):
            seen.update(kw)
            return {"message": {"content": "{}"}}
        self.file.write_text(json.dumps({"override_env": "BENCH_TEST_OVERRIDE", "jobs": {
            model._M["route_jobs"]["consult"]: {"model": "consult:1b"}}}), encoding="utf-8")
        fake = mock.MagicMock(chat=chat)
        with mock.patch.dict(sys.modules, {"ollama": fake}), model.job("consult"):
            model.ollama_call("s", "u")
        self.assertEqual(seen["model"], "consult:1b")

    def test_local_team_and_autopilot_follow_routing(self):
        from orchestra.local_team import core as lt
        from orchestra.local_team.settings import load
        self.file.write_text(json.dumps({"override_env": "BENCH_TEST_OVERRIDE", "jobs": {
            load()["routing_job"]: {"model": "team:1b"}}}), encoding="utf-8")
        ctx = lt.Ctx(ROOT)
        self.assertEqual(lt.model_name(ctx), "team:1b")
        self.file.write_text(json.dumps({"jobs": {}}), encoding="utf-8")
        self.assertEqual(lt.model_name(ctx), ctx.cfg["local_team_model"])


class TestConfig(unittest.TestCase):
    def test_every_bench_job_has_a_routing_entry_and_a_kind(self):
        routing = model_routing.jobs()
        for name, spec in CFG["jobs"].items():
            if name.startswith("_"):
                continue
            self.assertIn(name, routing)
            self.assertIn(spec["kind"], jobs.KINDS)
            self.assertIn(spec["lease"], ("bench", "own"))

    def test_routing_ships_with_todays_models(self):
        self.assertTrue(all(v.get("model") is None for v in model_routing.jobs().values()))

    def test_tutor_route_jobs_name_routing_jobs(self):
        from tutor import model
        self.assertTrue(set(model._M["route_jobs"].values()) <= set(model_routing.jobs()))

    def test_eval_case_ids_exist(self):
        from eval.cases import CASES
        ids = {c["id"] for c in CASES}
        self.assertEqual(set(CFG["jobs"]["denisse_brain"]["only"]) - ids, set())

    def test_local_team_fixtures_exist(self):
        from orchestra.local_team import baseline
        names = {f["name"] for f in baseline.load_fixtures()}
        self.assertEqual(set(CFG["jobs"]["orchestra_local_team"]["only"]) - names, set())

    def test_fixtures_load(self):
        for name, spec in CFG["jobs"].items():
            if not name.startswith("_") and spec.get("fixtures"):
                self.assertTrue(jobs.fixtures(spec), name)

    def test_unknown_job_is_refused(self):
        with self.assertRaises(ValueError):
            core.job_names(CFG, "don_extract,nope")


class TestMeter(unittest.TestCase):
    def test_ollama_client_calls_are_counted(self):
        fake = mock.MagicMock()
        fake.chat = lambda **kw: {"eval_count": 50, "eval_duration": 2_000_000_000, "prompt_eval_count": 10}
        with mock.patch.dict(sys.modules, {"ollama": fake}):
            with meter.metered("/api/chat") as prof:
                fake.chat(model="m")
            self.assertEqual(meter.figures(prof)["tokens_per_s"], 25.0)
            self.assertEqual(prof.prompt_tokens, 10)

    def test_http_chat_calls_are_counted_and_the_reply_is_unchanged(self):
        body = json.dumps({"message": {"content": "hi"}, "eval_count": 30, "eval_duration": 1_000_000_000}).encode()

        class Resp:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body
        with mock.patch.object(urllib.request, "urlopen", lambda *a, **k: Resp()):
            with meter.metered("/api/chat") as prof:
                with urllib.request.urlopen("http://x/api/chat") as r:
                    self.assertEqual(r.read(), body)
            self.assertEqual(meter.figures(prof)["tokens_per_s"], 30.0)

    def test_unreachable_ollama_gives_no_vram_and_no_tags(self):
        def refuse(*a, **k):
            raise OSError("refused")
        with mock.patch.object(urllib.request, "urlopen", refuse):
            self.assertEqual(meter.vram("http://x/api/ps", "m", 1), {"vram_mib": None, "size_mib": None})
            self.assertIsNone(meter.installed("http://x/api/tags", 1))


class TestScore(unittest.TestCase):
    def test_grounded_share(self):
        self.assertEqual(score.grounded_share("capacitor charges", "the capacitor charges slowly", 4), 1.0)
        self.assertEqual(score.grounded_share("banana", "the capacitor", 4), 0.0)

    def test_terms_ignore_case_and_spacing(self):
        self.assertTrue(score.has_terms("The  Time\nConstant", ["time constant"]))
        self.assertFalse(score.has_terms("time", ["time constant"]))

    def test_case_and_summary(self):
        c = score.case("a", {"x": True, "y": False}, 1.234)
        self.assertEqual((c["score"], c["ok"], c["seconds"]), (0.5, False, 1.23))
        s = score.summarise([c, score.case("b", {"x": True}, 3.0)])
        self.assertEqual((s["score"], s["ok"], s["cases"], s["median_s"]), (0.75, 1, 2, 2.12))


if __name__ == "__main__":
    unittest.main()
