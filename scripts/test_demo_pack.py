"""Offline checks for the evidence pack (demo/). No Ollama, GPU or voice models.

    .venv\\Scripts\\python.exe -m unittest discover -s scripts -p "test_demo_pack.py" -v
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

import evidence  # noqa: E402
import report  # noqa: E402
import run_demo  # noqa: E402

PY = sys.executable


def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


class Scoring(unittest.TestCase):
    def test_pass_hat_k(self):
        self.assertEqual(evidence.pass_hat_k(3, 3, 3), 1.0)
        self.assertEqual(evidence.pass_hat_k(2, 3, 3), 0.0)
        self.assertAlmostEqual(evidence.pass_hat_k(2, 3, 1), 2 / 3)
        self.assertAlmostEqual(evidence.pass_hat_k(3, 4, 2), 0.5)  # C(3,2)/C(4,2)
        self.assertEqual(evidence.pass_hat_k(0, 3, 1), 0.0)
        self.assertEqual(evidence.pass_hat_k(1, 0, 1), 0.0)
        self.assertEqual(evidence.pass_hat_k(1, 2, 3), 0.0)

    def test_per_case_and_summary(self):
        recs = [{"id": "a", "passed": True, "failures": []},
                {"id": "b", "passed": False, "failures": ["missing 'x'"]},
                {"id": "a", "passed": True, "failures": []},
                {"id": "b", "passed": True, "failures": []},
                {"id": "a", "passed": True, "failures": []},
                {"id": "b", "passed": False, "failures": ["missing 'x'"]}]
        cases = evidence.per_case(recs)
        self.assertEqual([c["id"] for c in cases], ["a", "b"])
        a, b = cases
        self.assertEqual((a["passes"], a["attempts"], a["pass_hat_k"]), (3, 3, 1.0))
        self.assertEqual((b["passes"], b["attempts"], b["pass_hat_k"]), (1, 3, 0.0))
        self.assertEqual(b["failures"], ["missing 'x'"])  # deduplicated
        s = evidence.summarize(cases)
        self.assertEqual((s["cases"], s["attempts"], s["passes"], s["cases_all_pass"], s["k"]), (2, 6, 4, 1, 3))
        self.assertEqual(s["pass_hat_k"], 0.5)
        self.assertEqual(s["pass_at_1"], round(4 / 6, 3))
        self.assertEqual(evidence.summarize([])["cases"], 0)

    def test_redact_paths(self):
        out = evidence.redact(r"invented path(s) ['C:\Users\someone\Documents\Passport.pdf'] and /home/x/y")
        self.assertNotIn("someone", out)
        self.assertNotIn("/home/x", out)
        self.assertIn("<path>", out)
        self.assertEqual(evidence.redact("missing 'dentist'"), "missing 'dentist'")


class Shaping(unittest.TestCase):
    def test_reply_text_never_kept(self):
        secret = "SECRET-REPLY-TEXT"
        suite = evidence.shape_suite([{"round": 1, "id": "x", "passed": False, "tools": [], "seconds": 2.0,
                                       "failures": [r"invented path(s) ['C:\Users\a\b.pdf']"], "robotic": True,
                                       "reply": secret}])
        dialogue = evidence.shape_dialogue([{"trial": 1, "id": "d", "passed": True, "tools": ["find_files"],
                                             "failures": [], "reply": secret}])
        actions = evidence.shape_actions([{"id": "t", "trial": 1, "passed": False, "status": "failed",
                                           "error": "Step budget exhausted", "suggestion": secret, "steps": [secret]}])
        learning = evidence.shape_adaptive([{"trial": 1, "learning": True, "prompt": "p", "tools": [],
                                            "reply": secret, "passed": False}])
        for shaped in (suite, dialogue, actions, learning):
            self.assertNotIn(secret, json.dumps(shaped))
        self.assertNotIn(r"C:\Users", json.dumps(suite))
        self.assertEqual(suite["summary"]["robotic_replies"], 1)
        self.assertEqual(suite["summary"]["median_seconds"], 2.0)
        self.assertIn("status=failed: Step budget exhausted", actions["cases"][0]["failures"])
        self.assertEqual(learning["summary"]["learning_on"]["attempts"], 1)
        self.assertEqual(learning["cases"][0]["failures"], ["check failed; tools called: no tools"])

    def test_voice_summary(self):
        out = {"whisper_model": "small.en", "whisper_mean_wer": 0.05, "tts_mean_s": 0.4,
               "rows": [{"text": "hi", "tts_s": 0.4, "whisper": "hi", "whisper_wer": 0.0, "whisper_s": 0.2}]}
        v = evidence.shape_voice(out)
        self.assertEqual(v["summary"]["sentences"], 1)
        self.assertEqual(v["summary"]["whisper_mean_wer"], 0.05)


class Runner(unittest.TestCase):
    def test_run_part_reads_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.json"
            code = f"import json;open(r'{out}','w').write(json.dumps([{{'id':'a','passed':True,'failures':[]}}]))"
            entry = run_demo.run_part("x", [PY, "-c", code], out, evidence.shape_actions, 60)
        self.assertEqual(entry["status"], "ran")
        self.assertEqual(entry["summary"]["passes"], 1)

    def test_run_part_missing_output_is_error_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.json"
            out.write_text("[]")  # a stale file from an earlier run must not count
            entry = run_demo.run_part("x", [PY, "-c", "import sys;sys.exit('boom')"], out,
                                      evidence.shape_actions, 60)
        self.assertEqual(entry["status"], "error")
        self.assertIn("boom", entry["reason"])
        self.assertNotIn("summary", entry)

    def test_run_part_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = run_demo.run_part("x", [PY, "-c", "import time;time.sleep(30)"], Path(tmp) / "o.json",
                                      evidence.shape_actions, 1)
        self.assertEqual(entry["status"], "error")
        self.assertIn("timed out", entry["reason"])

    def test_everything_skipped_without_ollama_or_voice(self):
        fake_machine = {"model": "llama3.1:8b", "ollama_version": None,
                        "ollama_note": "Ollama not reachable at http://127.0.0.1:11434: URLError"}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(run_demo, "machine", lambda py: dict(fake_machine)), \
                patch.object(run_demo, "voice_missing", lambda env: "Kokoro model files not found"), \
                patch.object(run_demo, "run_part", side_effect=AssertionError("nothing should run")):
            out = Path(tmp) / "r.json"
            self.assertEqual(run_demo.main(["--out", str(out), "--no-report"]), 0)
            data = json.loads(out.read_text(encoding="utf-8"))
        for name in run_demo.PARTS:
            self.assertEqual(data["parts"][name]["status"], "skipped", name)
            self.assertTrue(data["parts"][name]["reason"])
            self.assertNotIn("summary", data["parts"][name])
        self.assertIn("Ollama not reachable", data["parts"]["suite"]["reason"])
        self.assertIn("Kokoro", data["parts"]["voice"]["reason"])
        self.assertTrue(data["git"]["commit"])

    def test_skip_flag(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(run_demo, "machine", lambda py: {"ollama_version": "x", "model": "m"}), \
                patch.object(run_demo, "run_part", side_effect=AssertionError("nothing should run")):
            out = Path(tmp) / "r.json"
            run_demo.main(["--out", str(out), "--no-report", "--skip", *run_demo.PARTS])
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(all(p["reason"] == "--skip on the command line" for p in data["parts"].values()))


def _fixture():
    suite = evidence.shape_suite(
        [{"round": r, "id": "greet", "passed": True, "tools": [], "seconds": 1.0, "failures": []} for r in (1, 2, 3)]
        + [{"round": r, "id": "task_add", "passed": r != 2, "tools": ["add_task"], "seconds": 3.0,
            "failures": [] if r != 2 else ["missing 'dentist'"]} for r in (1, 2, 3)])
    return {"generated": "2026-11-02T20:00:00+00:00", "finished": "2026-11-02T20:30:00+00:00", "rounds": 3,
            "command": "python demo/run_demo.py", "git": {"commit": "abcdef1234", "branch": "b"},
            "machine": {"model": "llama3.1:8b", "gpu": "Test GPU", "num_ctx": 8192, "ollama_version": "0.1"},
            "parts": {"suite": {"status": "ran", "seconds": 12.5, **suite},
                      "voice": {"status": "skipped", "reason": "Kokoro model files not found"}}}


class Report(unittest.TestCase):
    def test_render(self):
        res = ROOT / "demo" / "results" / "2026-11-02.json"
        md = report.render(_fixture(), res, ROOT / "docs" / "denisse_evidence.md")
        self.assertIn("../demo/results/2026-11-02.json", md)
        self.assertIn("Not measured: skipped** — Kokoro model files not found", md)
        self.assertIn("`task_add` — 2/3: missing 'dentist'", md)
        self.assertIn("demo\\run_demo.py --rounds 3", md)
        link = "](../demo/results/2026-11-02.json)"
        self.assertIn(f"[1{link} of [2{link} prompt-suite cases passed all [3{link} attempts", md)
        # dialogue/actions/learning absent from the file -> reported, not invented
        self.assertEqual(md.count("Not measured: skipped** — not in results file"), 3)
        self.assertIn("Not measured in this run: dialogue", md)
        # every value cell holding a number in a Measure table is a link to the results
        in_table = False
        for line in md.splitlines():
            if line.startswith("| Measure |"):
                in_table = True
                continue
            if in_table and not line.startswith("|"):
                in_table = False
            if in_table and not line.startswith("|---"):
                value = line.split("|")[2]
                if re.search(r"\d", value):
                    self.assertIn("](../demo/results/2026-11-02.json)", value, line)
        for banned in ("revolutionary", "cutting-edge", "seamless", "blazing"):
            self.assertNotIn(banned, md.lower())


class Committed(unittest.TestCase):
    """The committed pack must be private-data clean and in sync."""

    def setUp(self):
        self.files = sorted((ROOT / "demo" / "results").glob("*.json"))

    def test_results_hold_no_reply_text_or_paths(self):
        for f in self.files:
            text = f.read_text(encoding="utf-8")
            data = json.loads(text)
            keys = {k for k, _ in _walk(data)}
            self.assertFalse(keys & {"reply", "suggestion", "steps", "evidence", "whisper", "vosk"}, f.name)
            self.assertNotRegex(text, r"[A-Za-z]:\\\\Users\\\\", f.name)
            for name in run_demo.PARTS:
                part = data["parts"][name]
                self.assertIn(part["status"], ("ran", "skipped", "error"))
                if part["status"] != "ran":
                    self.assertTrue(part.get("reason"), f"{f.name} {name} has no reason")

    def test_rendered_doc_matches_latest_results(self):
        if not self.files:
            self.skipTest("no committed results yet")
        doc = ROOT / "docs" / "denisse_evidence.md"
        expected = report.render(json.loads(self.files[-1].read_text(encoding="utf-8")), self.files[-1], doc)
        self.assertEqual(doc.read_text(encoding="utf-8"), expected,
                         "docs/denisse_evidence.md is stale: run demo/report.py")

    def test_docs_linked(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/denisse_evidence.md", readme)
        self.assertIn("docs/architecture.md", readme)
        arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        self.assertIn("```mermaid", arch)
        for part in ("tool_router", "instincts", "honesty", "adaptive", "study", "vault", "voice",
                     "Orchestra", "Atrium"):
            self.assertIn(part.lower(), arch.lower(), part)


if __name__ == "__main__":
    unittest.main()
