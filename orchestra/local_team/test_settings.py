"""Local-team policy edits, isolated from real Ollama, leases and repositories."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from orchestra.local_team import baseline, core, lease, roles, settings


class LocalSettings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "orchestra").mkdir()
        (self.root / "orchestra/config.json").write_text("{}", encoding="utf-8")
        self.policy = settings.load()
        self.file = self.root / "rules.json"
        self.save()
        patch = mock.patch.object(settings, "DATA_FILE", self.file)
        patch.start()
        self.addCleanup(patch.stop)

    def save(self):
        self.file.write_text(json.dumps(self.policy), encoding="utf-8")

    def test_config_defaults_and_explicit_override(self):
        self.policy["defaults"].update(local_team_timeout_s=11, producer_plans_per_run=4)
        self.save()
        ctx = core.Ctx(self.root, cfg={"producer_plans_per_run": 1})
        self.assertEqual(ctx.cfg["local_team_timeout_s"], 11)
        self.assertEqual(ctx.cfg["producer_plans_per_run"], 1)

    def test_role_prompt_is_loaded_from_json(self):
        self.policy["role_prompts"]["scout"] = "A different scout instruction."
        self.save()
        self.assertEqual(core.load_role("scout").prompt, "A different scout instruction.")

    def test_scout_signals_and_bounds(self):
        ctx = core.Ctx(self.root)
        ctx.review.mkdir()
        (ctx.review / "runner.log").write_text("FAIL old\nNOTICE one\nNOTICE two\n", encoding="utf-8")
        self.assertEqual([i["text"] for i in roles.collect(ctx, {})[0]], ["FAIL old"])
        self.policy["collection"].update(log_signal_pattern="NOTICE", max_items=1, item_chars=8)
        self.save()
        self.assertEqual([i["text"] for i in roles.collect(ctx, {})[0]], ["NOTICE t"])

    def test_planner_priority_and_test_file_limits(self):
        cands = [{"worth": w} for w in ("high", "medium", "low")]
        self.policy["planner"].update(worth_order=["low", "medium", "high"], ignored_worth=["high"],
                                       test_globs=["checks/*.py"], max_test_files=1)
        self.save()
        self.assertEqual(roles.ranked_candidates(cands), [cands[2], cands[1]])
        (self.root / "checks").mkdir()
        for name in ("a", "b"):
            (self.root / f"checks/{name}.py").touch()
        self.assertEqual(roles._test_files(self.root), ["checks/a.py"])

    def test_librarian_payload_filter_and_history(self):
        ctx = core.Ctx(self.root)
        report = self.root / "fixture.report.md"
        report.write_text("RESULT: PASS\nDONE:\n- Example fact.\n", encoding="utf-8")
        self.policy["librarian"].update(report_chars=12, private_pattern="withheld", history_count=1)
        self.save()
        answer = {"lines": [{"text": "A retained fact", "section": "DONE"},
                             {"text": "A withheld fact", "section": "DONE"}]}
        with mock.patch.object(core, "ask", return_value=answer) as ask, contextlib.redirect_stdout(io.StringIO()):
            result = roles.librarian(ctx, report)
        self.assertEqual(ask.call_args.args[2]["report"], "RESULT: PASS")
        self.assertEqual(len(result["lines"]), 1)
        roles.mark_librarian_done(ctx, report)
        roles.mark_librarian_done(ctx, self.root / "next.report.md")
        self.assertEqual(ctx.state()["librarian"]["done"], ["next.report.md"])

    def test_probe_and_gpu_settle_timings(self):
        self.policy["ollama"]["probe_timeout_s"] = 7
        self.policy["gpu"].update(settle_window_s=30, settle_delay_s=4, probe_timeout_s=6)
        self.save()
        ctx = core.Ctx(self.root)
        with mock.patch.object(core.urllib.request, "urlopen", side_effect=OSError) as request:
            with self.assertRaises(OSError):
                core._get(ctx, "/api/tags")
        self.assertEqual(request.call_args.kwargs["timeout"], 7)
        with mock.patch.object(core.shutil, "which", return_value="nvidia-smi"), \
                mock.patch.object(core.subprocess, "run", return_value=mock.Mock(stdout="5")) as run:
            self.assertEqual(core.gpu_utilisation(), 5)
        self.assertEqual(run.call_args.kwargs["timeout"], 6)
        with mock.patch.object(core, "_get", return_value={"models": [{"name": ctx.cfg["local_team_model"]}]}), \
                mock.patch.object(core, "gpu_utilisation", side_effect=[99, 0]), \
                mock.patch.object(core, "_last_call_end", 80), mock.patch.object(core.time, "monotonic", return_value=100), \
                mock.patch.object(core.time, "sleep") as sleep:
            core.ready(ctx)
        sleep.assert_called_once_with(4)

    def test_lease_defaults_polling_and_attempt_count(self):
        self.policy["defaults"]["gpu_lease_ttl_s"] = 17
        self.policy["lease"].update(poll_s=0.5, acquire_attempts=1)
        self.save()
        held = lease.GpuLease(self.root / "lease", "fixture")
        self.assertEqual(held.ttl_s, 17)
        with mock.patch.object(held, "try_acquire", side_effect=[False, True]), \
                mock.patch.object(lease.time, "sleep") as sleep:
            self.assertTrue(held.acquire(wait_s=10))
        sleep.assert_called_once_with(0.5)
        with mock.patch.object(lease.os, "open", side_effect=FileExistsError) as opened, \
                mock.patch.object(held, "info", return_value=None):
            self.assertFalse(held.try_acquire())
        self.assertEqual(opened.call_count, 1)

    def test_baseline_cli_run_counts_and_wait(self):
        self.policy["baseline"].update(rounds=4, plans=1, wait_gpu_s=7)
        self.save()
        with mock.patch.object(baseline, "run", return_value={"score": {}, "lease_probes": []}) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(baseline.main(["--out", str(self.root)]), 0)
        self.assertEqual(run.call_args.args[:3], (4, None, 1))
        self.assertEqual(run.call_args.kwargs["wait_gpu_s"], 7)


if __name__ == "__main__":
    unittest.main()
