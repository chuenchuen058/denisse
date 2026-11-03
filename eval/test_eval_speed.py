"""eval/speed.py and the run_eval flags built on it (2026-11-03). No model,
no git, no nvidia-smi: every input is fed in."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import run_eval, speed  # noqa: E402
from eval.cases import CASES  # noqa: E402

BY_ID = {c["id"]: c for c in CASES}


class Selection(unittest.TestCase):
    def ids(self, changed):
        chosen, reasons = speed.select_cases(CASES, changed=changed)
        return [c["id"] for c in chosen], reasons

    def test_smoke_cases_exist(self):
        for cid in speed.SMOKE:
            self.assertIn(cid, BY_ID, cid)

    def test_nothing_changed_is_the_smoke_set(self):
        ids, _ = self.ids([])
        self.assertEqual(sorted(ids), sorted(speed.SMOKE))

    def test_docs_and_tests_select_nothing_extra(self):
        ids, reasons = self.ids(["docs/eval_speed.md", "scripts/test_tasks.py", "orchestra/runner.py"])
        self.assertEqual(sorted(ids), sorted(speed.SMOKE))

    def test_core_file_is_the_full_suite(self):
        for path in ("brain.py", "tool_router.py", "eval/cases.py", "eval/run_eval.py"):
            ids, reasons = self.ids([path])
            self.assertEqual(len(ids), len(CASES), path)
            self.assertTrue(any("full suite" in r for r in reasons), reasons)

    def test_timers_select_timer_cases_only(self):
        ids, _ = self.ids(["timers.py"])
        for cid in ("timer_start", "timer_set", "timer_stop"):
            self.assertIn(cid, ids)
        self.assertNotIn("weather_follow_up", ids)
        self.assertNotIn("vault_lecturer", ids)
        self.assertLess(len(ids), len(CASES) // 2)

    def test_study_mode_selects_study_cases(self):
        ids, _ = self.ids(["study_mode.py"])
        for cid in ("study_start", "study_timer_cycles", "study_stop_clean"):
            self.assertIn(cid, ids)
        self.assertNotIn("news", ids)

    def test_vault_search_selects_vault_cases(self):
        ids, _ = self.ids(["vault_search.py"])
        for cid in ("vault_week_topic", "vault_lecturer", "my_projects"):
            self.assertIn(cid, ids)
        self.assertNotIn("timer_start", ids)

    def test_module_off_the_brain_path_selects_nothing(self):
        ids, reasons = self.ids(["tray.py"])
        self.assertEqual(sorted(ids), sorted(speed.SMOKE))
        self.assertTrue(any("not on the brain's path" in r for r in reasons), reasons)

    def test_unmapped_brain_import_is_the_full_suite(self):
        # speech_control is imported by brain and no case maps to it.
        self.assertIn("speech_control", speed.brain_imports())
        ids, reasons = self.ids(["speech_control.py"])
        self.assertEqual(len(ids), len(CASES))

    def test_suite_order_is_kept(self):
        ids, _ = self.ids(["timers.py"])
        order = [c["id"] for c in CASES]
        self.assertEqual(ids, sorted(ids, key=order.index))


class CaseModules(unittest.TestCase):
    def test_timer_case_maps_to_timers(self):
        self.assertIn("timers", speed.case_modules(BY_ID["timer_start"]))

    def test_weather_case_maps_to_tools(self):
        self.assertIn("tools", speed.case_modules(BY_ID["weather"]))

    def test_vault_fixture_case_maps_to_vault_search(self):
        self.assertIn("vault_search", speed.case_modules(BY_ID["vault_week_topic"]))

    def test_study_case_maps_to_study_mode(self):
        self.assertIn("study_mode", speed.case_modules(BY_ID["study_start"]))

    def test_briefing_maps_to_planner(self):
        self.assertIn("planner", speed.case_modules(BY_ID["briefing_real"]))

    def test_handoff_maps_to_delegate(self):
        self.assertIn("delegate", speed.case_modules(BY_ID["handoff_research"]))

    def test_greeting_maps_to_nothing_specific(self):
        self.assertFalse(speed.case_modules(BY_ID["greet_hey"]) & {"timers", "vault_search", "planner"})


class GpuReading(unittest.TestCase):
    def test_parse_one_gpu(self):
        self.assertEqual(speed.parse_gpu_csv("37, 5997, 8151\n"),
                         {"util_pct": 37, "used_mib": 5997, "total_mib": 8151})

    def test_parse_keeps_the_busiest(self):
        self.assertEqual(speed.parse_gpu_csv("3, 100, 8000\n80, 7000, 8000\n")["util_pct"], 80)

    def test_parse_garbage_is_none(self):
        self.assertIsNone(speed.parse_gpu_csv("[N/A], [N/A], [N/A]"))
        self.assertIsNone(speed.parse_gpu_csv(""))

    def test_noisy_threshold(self):
        self.assertTrue(speed.is_noisy({"util_pct": speed.NOISY_PCT}))
        self.assertFalse(speed.is_noisy({"util_pct": speed.NOISY_PCT - 1}))
        self.assertFalse(speed.is_noisy({"util_pct": None}))

    def test_header_lines_flag_noise_and_forced_lease(self):
        snap = {"util_pct": 97, "used_mib": 5999, "total_mib": 8151, "loaded": ["llama3.1:8b"], "noisy": True}
        text = "\n".join(speed.gpu_lines(snap, {"held": False, "holder": "orchestra.local_team librarian"}))
        self.assertIn("97% busy", text)
        self.assertIn("Noisy run", text)
        self.assertIn("not held", text)
        self.assertIn("librarian", text)

    def test_header_lines_quiet(self):
        snap = {"util_pct": 2, "used_mib": 5999, "total_mib": 8151, "loaded": [], "noisy": False}
        text = "\n".join(speed.gpu_lines(snap, {"held": True, "holder": "eval/run_eval.py"}))
        self.assertNotIn("Noisy", text)
        self.assertIn("held for the run", text)

    def test_unreadable_gpu_is_said_not_crashed(self):
        text = "\n".join(speed.gpu_lines({"util_pct": None, "note": "nvidia-smi not found", "loaded": []}, None))
        self.assertIn("unknown", text)
        self.assertIn("nvidia-smi not found", text)


class ProfileTotals(unittest.TestCase):
    def test_sums_ollama_fields_from_dicts_and_objects(self):
        p = speed.Profile()
        p.record_chat({"load_duration": 1e9, "prompt_eval_duration": 2e9, "eval_duration": 3e9,
                       "prompt_eval_count": 1000, "eval_count": 20}, wall=6.5)

        class R:
            load_duration = 0
            prompt_eval_duration = 1e9
            eval_duration = 1e9
            prompt_eval_count = 500
            eval_count = 50
        p.record_chat(R(), wall=2.0)
        p.record_tool("get_weather", 3.0)
        p.record_tool("find_files", 0.5)
        s = p.summary(wall_s=20, cases_s=15, startup_s=4, learning_s=1)
        self.assertEqual(s["calls"], 2)
        self.assertEqual(s["model_s"], 8.5)
        self.assertEqual((s["load_s"], s["prefill_s"], s["gen_s"]), (1.0, 3.0, 4.0))
        self.assertEqual((s["prompt_tokens"], s["gen_tokens"], s["max_gen_tokens"]), (1500, 70, 50))
        self.assertEqual((s["tool_calls"], s["tool_s"]), (2, 3.5))
        self.assertEqual(s["other_s"], 3.0)  # 15 - 8.5 - 3.5
        self.assertEqual(s["slowest_tools"][0], ("get_weather", 3.0))

    def test_delta_between_snapshots(self):
        p = speed.Profile()
        a = p.snapshot()
        p.record_chat({"eval_count": 7, "eval_duration": 1e9}, wall=1.25)
        d = speed.Profile.delta(a, p.snapshot())
        self.assertEqual((d["calls"], d["gen_tokens"], d["model_s"]), (1, 7, 1.25))

    def test_install_wraps_brain_calls_once(self):
        import brain
        real_chat, real_tool = brain.ollama.chat, brain._run_tool
        p = speed.Profile()
        try:
            with patch.object(brain.ollama, "chat", lambda **kw: {"eval_count": 3, "eval_duration": 5e8}):
                p.install()
                p.install()  # idempotent
                brain.ollama.chat(model="x", messages=[])
                self.assertEqual(p.calls, 1)
                self.assertEqual(p.gen_tokens, 3)
        finally:
            brain._run_tool = real_tool
            brain.ollama.chat = real_chat

    def test_timing_lines_render(self):
        s = speed.Profile().summary(wall_s=10, cases_s=8, startup_s=2)
        text = "\n".join(speed.timing_lines(s))
        self.assertIn("Where the time went", text)
        self.assertIn("| whole run (wall) | 10.0 |", text)


class ReportShape(unittest.TestCase):
    def results(self, **extra):
        r = {"id": "greet_hey", "prompt": "hey", "reply": "Hey.", "tools": [], "seconds": 0.0,
             "passed": True, "failures": [], "robotic": []}
        base = {"stamp": "2026-11-03 12:00", "date": "2026-11-03", "clean": [r], "live": [dict(r)],
                "clean_pass": 1, "live_pass": 1, "median": 0.0, "robotic": 0, "dataset_total": 0}
        base.update(extra)
        return base

    def test_subset_header_counts_the_subset_not_the_suite(self):
        text = run_eval.report(self.results(subset={"ref": "master", "reasons": ["timers.py: 3 case(s)"]}), 0)
        self.assertIn("(subset)", text)
        self.assertIn("1/1 passed", text)
        self.assertIn(f"subset: 1 of {len(CASES)} cases, changed since `master`", text)
        self.assertIn("timers.py: 3 case(s)", text)

    def test_full_run_report_has_gpu_and_timing(self):
        snap = {"util_pct": 55, "used_mib": 6000, "total_mib": 8151, "loaded": ["llama3.1:8b"], "noisy": True}
        timing = speed.Profile().summary(wall_s=1, cases_s=1, startup_s=0)
        text = run_eval.report(self.results(gpu=snap, lease={"held": True, "holder": "x"}, timing=timing), 0)
        self.assertIn("GPU at start: 55% busy", text)
        self.assertIn("Noisy run", text)
        self.assertIn("Where the time went", text)
        self.assertNotIn("(subset)", text)

    def test_old_results_without_new_keys_still_render(self):
        text = run_eval.report(self.results(), 0)
        self.assertIn("# Denisse eval", text)
        self.assertNotIn("GPU", text)


class LeaseGate(unittest.TestCase):
    """A held lease stops the run (exit 3) unless told to run anyway."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="denisse-lease-test-")
        self.path = Path(self.dir) / "gpu.lease"
        # Held by this very process, so it is never judged stale.
        self.path.write_text(json.dumps({"pid": os.getpid(), "holder": "orchestra.local_team librarian",
                                         "ttl_s": 900, "since": "2099-01-01T00:00:00"}), encoding="utf-8")
        self.env = patch.dict(os.environ, {"ORCHESTRA_GPU_LEASE": str(self.path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.path.unlink(missing_ok=True)
        os.rmdir(self.dir)

    def test_busy_exits_3_by_default(self):
        with self.assertRaises(SystemExit) as cm:
            run_eval._gpu_lease(wait_s=0, on_busy="exit")
        self.assertEqual(cm.exception.code, 3)
        self.assertTrue(self.path.exists(), "someone else's lease must be left alone")

    def test_force_runs_without_the_lease(self):
        self.assertIsNone(run_eval._gpu_lease(wait_s=0, on_busy="run"))
        self.assertEqual(run_eval.lease_info(None)["holder"], "orchestra.local_team librarian")

    def test_free_lease_is_taken_and_released(self):
        self.path.unlink()
        lease = run_eval._gpu_lease(wait_s=0, on_busy="exit")
        self.assertIsNotNone(lease)
        self.assertTrue(run_eval.lease_info(lease)["held"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["holder"], "eval/run_eval.py")
        lease.release()
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
