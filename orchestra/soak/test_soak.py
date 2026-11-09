"""Tests for the soak harness itself, plus one short soak that must hold every invariant.

    .venv\\Scripts\\python.exe -m unittest discover -s orchestra/soak -p "test_*.py" -v
"""
# checks: slow  (~50s: a real mini-soak with concurrent tick processes and kills)
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import fake_lane  # noqa: E402
import harness  # noqa: E402


class PlansTest(unittest.TestCase):
    def test_plans_are_deterministic_and_cover_every_path(self):
        a, b = harness.make_plans(1, 40), harness.make_plans(1, 40)
        self.assertEqual(a, b)
        self.assertNotEqual(a, harness.make_plans(2, 40))
        results = {p["result"] for p in a}
        self.assertTrue({"PASS", "PARTIAL", "FAIL", "NONE"} <= results, results)
        for key in ("limit", "hot", "risky", "budget", "bad_gate", "grandchild", "stray", "private", "human"):
            self.assertTrue(any(p.get(key) for p in a), f"no plan with {key}")
        self.assertEqual(a[-1]["sleep"], 300, "the last task hangs past its timeout")
        self.assertEqual(len({p["slug"] for p in a}), 40)

    def test_task_text_is_a_runner_task_and_the_lane_reads_the_plan_back(self):
        import runner as R
        plan = harness.make_plans(3, 12)[5]
        text = harness.task_text(plan, 5, 3, sys.executable)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / f"{plan['slug']}.md"
            p.write_text(text, encoding="utf-8")
            task = R.parse_task(p)
        self.assertEqual(task.fields["queue"], "auto")
        self.assertEqual(task.lane, plan["lane"])
        self.assertEqual(len(task.gate_commands), 1)
        self.assertIn("soak_gate.py", task.gate_commands[0])
        back = fake_lane.plan_from(text)
        self.assertEqual(back["slug"], plan["slug"])
        self.assertEqual(back["result"], plan["result"])

    def test_gate_script_flags_conflict_markers_broken_flag_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate = root / "soak_gate.py"
            gate.write_text(harness.GATE_SCRIPT, encoding="utf-8")
            run = lambda: subprocess.run([sys.executable, str(gate)], cwd=root, capture_output=True, text=True)
            (root / "hot.md").write_text("# hot\nline\n", encoding="utf-8")
            self.assertEqual(run().returncode, 0)
            (root / "hot.md").write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> branch\n", encoding="utf-8")
            self.assertEqual(run().returncode, 1)
            (root / "hot.md").write_text("# hot\n", encoding="utf-8")
            (root / "budget").mkdir()
            for i in range(3):
                (root / "budget" / f"{i}.txt").write_text("1\n")
            r = run()
            self.assertEqual(r.returncode, 1)
            self.assertIn("budget/ has 3", r.stdout)


SLOW_ENV = "RUN_CHECKS_SLOW"  # scripts/run_checks.py SLOW_ENV, set by a run without --fast


@unittest.skipUnless(os.environ.get(SLOW_ENV), f"slow (~35-50s): set {SLOW_ENV}=1, or run "
                     "scripts/run_checks.py without --fast")
class ShortSoakTest(unittest.TestCase):
    def test_short_soak_holds_every_invariant(self):
        soak = harness.Soak(minutes=0.6, seed=2, tasks=8, procs=3, kill_every=6, flip_every=5, quiet=True)
        try:
            soak.setup()
            soak.run()
            findings = soak.check()
            report = soak.report()
            broken = [f for f in findings if not f["ok"]]
            self.assertEqual(broken, [], report)
            s = soak.summary()
            self.assertGreaterEqual(s["lane_starts"], 4, report)
            self.assertGreaterEqual(s["ticks"], 10, report)
            self.assertEqual(set(f["name"] for f in findings), set(harness.INVARIANTS))
            # Every one of the 8 tasks is still somewhere (queue, parked or archive). A PASS that needed
            # a merge by hand also queues a <slug><suffix> follow-up (conflicts.py): that is the same
            # task going round again, not a 9th one, so it counts as its original.
            suffix = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8")) \
                .get("conflict_followup_suffix", "")
            files = [*(soak.review / "queue").glob("*.md"), *(soak.review / "parked").glob("*.md"),
                     *(soak.review / "archive").glob("*/*.md")]
            stems = {p.stem for p in files if "." not in p.stem}  # task files, not x.report / x.why
            originals = {st[:-len(suffix)] if suffix and st.endswith(suffix) else st for st in stems}
            self.assertEqual(len(originals), 8, report + f"\ntask files: {sorted(stems)}")
        finally:
            soak.cleanup()


if __name__ == "__main__":
    unittest.main()
