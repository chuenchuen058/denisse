"""Tests for orchestra/local_team/baseline.py and its fixtures, with a fake Ollama. The real
measurement (llama3.1:8b, 3 rounds) is in BASELINE.md; this checks the harness and the
fixture set, not the model.

    .venv\\Scripts\\python.exe -m unittest discover -s orchestra/local_team -p "test_*.py" -v
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from orchestra import admission  # noqa: E402
from orchestra.local_team import baseline, core  # noqa: E402
from orchestra.local_team.core import Skip  # noqa: E402
from orchestra.local_team.lease import GpuLease  # noqa: E402
from orchestra.local_team.test_local_team import FakeOllama  # noqa: E402

GOOD_PLAN = {"title": "Old hub job files are pruned after a day",
             "objective": "jobs() only filters job files older than 24 h; nothing deletes them. Prune them.",
             "benefit": "The jobs folder stops growing.",
             "scope_paths": ["orchestra/agent_hub/server.mjs", "orchestra/agent_hub/test_hub.mjs"],
             "acceptance": ["A job file older than 24 h is deleted on the next jobs() call."],
             "gate": {"id": "node-test", "arg": "orchestra/agent_hub/test_hub.mjs"}, "weight": "light"}

ORPHAN_PLAN = {"title": "Runner frees the slot of an orphan lane that never exits",
               "objective": "reconcile() re-checks an orphan lane every tick but never times it out, so a hung "
                            "orphan blocks its slot until someone kills it. Give orphans the lane timeout.",
               "benefit": "A hung lane no longer stalls the queue.",
               "scope_paths": ["orchestra/runner.py", "orchestra/test_runner.py"],
               "acceptance": ["An orphan lane past its timeout is killed and its slot freed."],
               "gate": {"id": "unittest", "arg": "orchestra.test_runner"}, "weight": "heavy"}


class FixtureSetTest(unittest.TestCase):
    def test_twelve_labelled_fixtures_with_registered_gates(self):
        fx = baseline.load_fixtures()
        self.assertEqual(len(fx), 12)
        gates = admission.load_gates(REPO_ROOT)
        self.assertGreaterEqual(sum(f["expected"]["candidate"] for f in fx), 4)
        self.assertGreaterEqual(sum(not f["expected"]["candidate"] for f in fx), 4)
        for f in fx:
            e = f["expected"]
            self.assertTrue((f["dir"] / "review").is_dir(), f["name"])
            self.assertTrue(e.get("why"), f["name"])
            if not e["candidate"]:
                self.assertEqual(e["gates"], [], f["name"])
                continue
            self.assertIn(e["area"], gates["areas"], f["name"])
            self.assertTrue(e["gates"], f["name"])
            for g in e["gates"]:  # every expected gate must be one admission would render
                cmd, bad = admission.render_gate(g, e["area"], REPO_ROOT, gates)
                self.assertFalse(bad, f"{f['name']}: {bad}")

    def test_fixtures_hold_nothing_personal(self):
        for p in baseline.FIXTURES.rglob("*"):
            if p.is_file():
                text = p.read_text(encoding="utf-8")
                for needle in ("Xichu", "King's College", "@gmail", "OneDrive - "):
                    self.assertNotIn(needle, text, p)


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = FakeOllama()
        patches = [mock.patch.object(core, "gpu_utilisation", return_value=None),
                   mock.patch.dict(os.environ, {"ORCHESTRA_GPU_LEASE": str(self.tmp / "gpu.lease")})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.files = baseline.tracked_files(baseline.SRC_REPO)

    def tearDown(self):
        self.fake.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fixture(self, name):
        return next(f for f in baseline.load_fixtures() if f["name"] == name)

    def test_build_repo_has_the_tree_and_the_fixture_review(self):
        fx = self.fixture("02-hub-jobs-pruning")
        repo = baseline.build_repo(fx, self.tmp / "r", files=self.files)
        self.assertTrue((repo / "orchestra" / "agent_hub" / "test_hub.mjs").is_file())
        self.assertTrue(any((repo / ".review" / "archive").rglob("*.report.md")))
        cfg = json.loads((repo / "orchestra" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["auto_admit_areas"], [])
        self.assertFalse((repo / "orchestra" / "local_team" / "fixtures").exists())

    def test_fixture_run_scores_admission_and_duplicates(self):
        self.fake.answers["scout"] = [{"candidates": [
            {"problem": "An orphan lane that never exits keeps its slot forever.", "evidence": ["E1"],
             "area_hint": "orchestra", "worth": "high"}], "nothing_because": ""}]
        self.fake.answers["planner"] = [ORPHAN_PLAN]
        fx = self.fixture("03-orphan-lane-timeout")
        r = baseline.run_fixture(fx, baseline.build_repo(fx, self.tmp / "a", files=self.files), ollama=self.fake.url)
        self.assertEqual(r["plans"][0]["admission"]["kind"], "admitted", r)
        self.assertEqual(r["plans"][0]["area"], "orchestra")

        # the hub pruning gap was closed by an archived task: a re-proposal is a duplicate
        dup = self.fixture("02-hub-jobs-pruning")
        self.fake.answers["scout"] = [{"candidates": [
            {"problem": "Nothing prunes old job files in .review/hub/jobs.", "evidence": ["E2"],
             "area_hint": "orchestra", "worth": "medium"}], "nothing_because": ""}]
        self.fake.answers["planner"] = [GOOD_PLAN]
        d = baseline.run_fixture(dup, baseline.build_repo(dup, self.tmp / "c", files=self.files), ollama=self.fake.url)
        self.assertEqual(d["plans"][0]["admission"]["kind"], "duplicate", d)

        # a negative fixture where the scout correctly finds nothing
        neg = self.fixture("11-done-as-asked")
        self.fake.answers["scout"] = [{"candidates": [], "nothing_because": "all done as asked"}]
        q = baseline.run_fixture(neg, baseline.build_repo(neg, self.tmp / "b", files=self.files), ollama=self.fake.url)
        self.assertEqual(q["candidates"], [])
        s = baseline.score([r, d, q], [fx, dup, neg])
        self.assertEqual(s["fixture_accuracy_pct"], 100)
        self.assertEqual(s["admission_pass_pct"], 50)
        self.assertEqual(s["candidate_precision_pct"], 100)
        self.assertEqual((s["duplicate_fixture_packets"], s["duplicates_caught"]), (1, 1))
        self.assertEqual(s["gate_expected"], 2)
        self.assertEqual(s["false_admissions"], 0)
        self.assertEqual(s["per_fixture"], {"03-orphan-lane-timeout": ["hit"], "02-hub-jobs-pruning": ["hit"],
                                            "11-done-as-asked": ["quiet"]})

    def test_a_queued_task_makes_a_reproposal_a_duplicate(self):
        fx = self.fixture("07-local-team-baseline")
        self.fake.answers["scout"] = [{"candidates": [
            {"problem": "No measured quality baseline for llama3.1:8b proposals.", "evidence": ["E3"],
             "area_hint": "orchestra", "worth": "high"}], "nothing_because": ""}]
        self.fake.answers["planner"] = [dict(GOOD_PLAN, title="Local team: measure what llama3.1:8b proposals are worth",
                                             scope_paths=["orchestra/local_team/baseline.py", "orchestra/admission.py"],
                                             gate={"id": "unittest", "arg": "orchestra.test_admission"})]
        repo = baseline.build_repo(fx, self.tmp / "d", files=self.files)
        (repo / "orchestra" / "local_team" / "baseline.py").write_text("# stub\n", encoding="utf-8")
        r = baseline.run_fixture(fx, repo, ollama=self.fake.url)
        # admission.py is a policy file, so that refusal comes first; either way nothing is queued
        self.assertIn(r["plans"][0]["admission"]["kind"], ("duplicate", "path"))
        self.fake.answers["planner"] = [dict(GOOD_PLAN, title="Local team: measure what llama3.1:8b proposals are worth",
                                             scope_paths=["orchestra/local_team/baseline.py"],
                                             gate={"id": "unittest", "arg": "orchestra.test_admission"})]
        repo2 = baseline.build_repo(fx, self.tmp / "e", files=self.files)
        (repo2 / "orchestra" / "local_team" / "baseline.py").write_text("# stub\n", encoding="utf-8")
        r2 = baseline.run_fixture(fx, repo2, ollama=self.fake.url)
        self.assertEqual(r2["plans"][0]["admission"]["kind"], "duplicate", r2)

    def test_recorder_sees_the_lease_held_during_a_call_and_released_after(self):
        lease_path = self.tmp / "gpu.lease"
        rec = baseline.Recorder(lease_path)

        def slow_ask(ctx, role, payload):
            lease = GpuLease(lease_path, f"orchestra.local_team {role.name}")
            self.assertTrue(lease.try_acquire())
            try:
                time.sleep(0.7)
            finally:
                lease.release()
            return {"ok": 1}
        rec._orig = slow_ask
        role = core.load_role("scout")
        with rec.installed():
            core.ask(None, role, {})
        [call] = rec.calls
        self.assertGreaterEqual(call["lease_samples"], 2)
        self.assertTrue(call["lease_held"])
        self.assertTrue(call["released"])
        self.assertIsNot(core.ask, rec.ask, "the wrapper must be removed afterwards")

    def test_probe_skips_without_calling_the_model_while_the_lease_is_held(self):
        fx = self.fixture("03-orphan-lane-timeout")
        repo = baseline.build_repo(fx, self.tmp / "p", files=self.files)
        res = baseline.probe_lease(fx, repo, ollama=self.fake.url)
        self.assertTrue(res["respected"], res)
        self.assertEqual(self.fake.chats(), [])
        self.assertFalse((self.tmp / "gpu.lease").exists(), "the probe must release its lease")

    def test_wait_ready_gives_up_when_the_gpu_stays_busy(self):
        repo = baseline.build_repo(self.fixture("01-parked-generic-why"), self.tmp / "w", files=self.files)
        ctx = core.Ctx(repo, ollama=self.fake.url)
        with mock.patch.object(core, "gpu_utilisation", return_value=95):
            with self.assertRaises(Skip):
                baseline.wait_ready(ctx, max_wait_s=0, poll_s=0, log=lambda m: None)
        baseline.wait_ready(ctx, max_wait_s=0, poll_s=0, log=lambda m: None)  # free now: returns


if __name__ == "__main__":
    unittest.main()
