"""
scripts/model_bakeoff.py without Ollama: a fake CLI and a fake worker.

Guards the three things the bake-off is for: it refuses a model that isn't
installed (it must never pull one), pass^k means "every round", and a model
that spilled to the CPU is flagged rather than quietly reported as slow.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.model_bakeoff as mb  # noqa: E402

LIST = """NAME           ID              SIZE      MODIFIED
llama3.1:8b    46e0c10c039e    4.9 GB    2 months ago
qwen2.5:7b     845dbda0ea48    4.7 GB    1 day ago
"""
PS_GPU = """NAME           ID              SIZE      PROCESSOR    CONTEXT    UNTIL
llama3.1:8b    46e0c10c039e    5.8 GB    100% GPU     8192       Forever
"""
PS_SPILL = """NAME           ID              SIZE      PROCESSOR          CONTEXT    UNTIL
qwen2.5:7b     845dbda0ea48    9.1 GB    23%/77% CPU/GPU    8192       Forever
"""


def r(cid, passed, seconds=1.0, failures=None):
    return {"id": cid, "passed": passed, "seconds": seconds,
            "failures": failures or ([] if passed else ["missing 'x'"])}


class FakeOllama:
    def __init__(self, installed=("llama3.1:8b", "qwen2.5:7b"), loaded=()):
        self._installed = set(installed)
        self._loaded = [dict(x) for x in loaded]
        self.stopped = []

    def installed(self):
        return set(self._installed)

    def loaded(self):
        return [dict(x) for x in self._loaded]

    def stop(self, name):
        self.stopped.append(name)
        self._loaded = [x for x in self._loaded if x["name"] != name]

    def gpu(self):
        return {"used_mib": 6000, "total_mib": 8151}


class FakeSpawn:
    """Writes what a worker would, per model, without a brain."""

    def __init__(self, by_model):
        self.by_model = by_model
        self.calls = []

    def __call__(self, model, rounds, out):
        self.calls.append((model, rounds))
        rounds_data, probe = self.by_model[model]
        out.write_text(json.dumps({"model": model, "rounds": rounds_data[:rounds],
                                   "probe": probe}), encoding="utf-8")
        return 0


class Parsing(unittest.TestCase):
    def test_list(self):
        self.assertEqual(mb.parse_list(LIST), {"llama3.1:8b", "qwen2.5:7b"})
        self.assertEqual(mb.parse_list(""), set())

    def test_ps_on_gpu(self):
        [row] = mb.parse_ps(PS_GPU)
        self.assertEqual(row["name"], "llama3.1:8b")
        self.assertEqual(row["size_gb"], 5.8)
        self.assertEqual(row["processor"], "100% GPU")
        self.assertFalse(row["spilled"])

    def test_ps_flags_cpu_spill(self):
        [row] = mb.parse_ps(PS_SPILL)
        self.assertTrue(row["spilled"])
        self.assertEqual(row["size_gb"], 9.1)

    def test_spill_variants(self):
        self.assertFalse(mb.is_spilled("100% GPU"))
        self.assertTrue(mb.is_spilled("100% CPU"))
        self.assertTrue(mb.is_spilled("48%/52% CPU/GPU"))
        self.assertFalse(mb.is_spilled(""))

    def test_nvidia_smi(self):
        self.assertEqual(mb.parse_nvidia_smi("5999, 8151\n"),
                         {"used_mib": 5999, "total_mib": 8151})
        self.assertIsNone(mb.parse_nvidia_smi(""))


class Refusal(unittest.TestCase):
    def test_uninstalled_model_is_refused_before_any_run(self):
        spawn = FakeSpawn({})
        with tempfile.TemporaryDirectory() as d:
            rc = mb.bakeoff(["llama3.1:8b", "gemma9:7b"], 3, probe=FakeOllama(),
                            spawn=spawn, doc=Path(d) / "doc.md", raw_dir=Path(d))
            self.assertEqual(rc, 2)
            self.assertEqual(spawn.calls, [], "ran an eval despite a missing model")
            self.assertFalse((Path(d) / "doc.md").exists())

    def test_message_names_the_pull_but_does_not_run_it(self):
        msg = mb.refusal_message(["gemma9:7b"], {"llama3.1:8b"})
        self.assertIn("gemma9:7b", msg)
        self.assertIn("ollama pull gemma9:7b", msg)
        self.assertIn("never downloads", msg)

    def test_bare_name_resolves_to_latest(self):
        ok, refused = mb.check_installed(["mistral"], {"mistral:latest"})
        self.assertEqual((ok, refused), (["mistral:latest"], []))

    def test_no_ollama(self):
        with tempfile.TemporaryDirectory() as d:
            rc = mb.bakeoff(["llama3.1:8b"], 1, probe=FakeOllama(installed=()),
                            spawn=FakeSpawn({}), doc=Path(d) / "doc.md", raw_dir=Path(d))
        self.assertEqual(rc, 2)


class Aggregation(unittest.TestCase):
    def test_pass_k_needs_every_round(self):
        rounds = [
            [r("a", True, 1.0), r("b", True, 2.0), r("c", False, 3.0)],
            [r("a", True, 1.0), r("b", False, 2.0), r("c", False, 3.0)],
            [r("a", True, 1.0), r("b", True, 2.0), r("c", False, 3.0)],
        ]
        s = mb.aggregate("m", rounds)
        self.assertEqual(s["rounds"], 3)
        self.assertEqual(s["cases"], 3)
        self.assertEqual(s["pass_k"], 1)          # only a
        self.assertEqual(s["pass_any"], 2)        # a and b
        self.assertEqual(s["per_case"]["b"]["passes"], 2)
        self.assertFalse(s["per_case"]["b"]["pass_k"])
        self.assertTrue(s["per_case"]["a"]["pass_k"])
        self.assertEqual(s["mean_seconds"], 2.0)
        self.assertEqual(s["per_case"]["c"]["failures"], ["missing 'x'"])  # deduped

    def test_case_missing_from_a_round_is_not_pass_k(self):
        s = mb.aggregate("m", [[r("a", True), r("b", True)], [r("a", True)]])
        self.assertFalse(s["per_case"]["b"]["pass_k"])

    def test_probe_carries_spill(self):
        probe = dict(mb.parse_ps(PS_SPILL)[0], gpu=None)
        s = mb.aggregate("qwen2.5:7b", [[r("a", True)]], probe)
        self.assertTrue(s["spilled"])
        self.assertEqual(s["vram_gb"], 9.1)

    def test_diff(self):
        base = mb.aggregate("b", [[r("a", True), r("b", False), r("c", True)]])
        oth = mb.aggregate("o", [[r("a", True), r("b", True), r("c", False)]])
        self.assertEqual(mb.diff_vs(base, oth), {"gained": ["b"], "lost": ["c"]})


class EndToEnd(unittest.TestCase):
    def test_two_models_doc_and_raw(self):
        gpu_probe = dict(mb.parse_ps(PS_GPU)[0], gpu={"used_mib": 5999, "total_mib": 8151})
        spill_probe = dict(mb.parse_ps(PS_SPILL)[0], gpu=None)
        spawn = FakeSpawn({
            "llama3.1:8b": ([[r("a", True), r("b", False)]] * 3, gpu_probe),
            "qwen2.5:7b": ([[r("a", False), r("b", True)]] * 3, spill_probe),
        })
        fake = FakeOllama(loaded=[{"name": "llama3.1:8b"}, {"name": "other:1b"}])
        with tempfile.TemporaryDirectory() as d:
            doc, raw = Path(d) / "doc.md", Path(d) / "raw"
            rc = mb.bakeoff(["llama3.1:8b", "qwen2.5:7b"], 3, probe=fake, spawn=spawn,
                            doc=doc, raw_dir=raw)
            self.assertEqual(rc, 0)
            self.assertEqual(spawn.calls, [("llama3.1:8b", 3), ("qwen2.5:7b", 3)])
            # Other models are unloaded before measuring; the candidate is
            # unloaded afterwards; the default is left alone at the end.
            self.assertIn("other:1b", fake.stopped)
            self.assertEqual(fake.stopped[-1], "qwen2.5:7b")
            text = doc.read_text(encoding="utf-8")
            self.assertIn("| `llama3.1:8b` | 3 | 1/2 |", text)
            self.assertIn("**YES**", text)            # the spill is flagged
            self.assertIn("CPU spill", text)
            self.assertIn("Over the ~8 GB budget: qwen2.5:7b", text)
            self.assertIn("gained (1): `b`", text)
            self.assertIn("lost (1): `a`", text)
            self.assertIn("ollama pull qwen2.5:7b", text)
            self.assertIn("not verified here", text)
            [raw_file] = list(raw.glob("*.json"))
            data = json.loads(raw_file.read_text(encoding="utf-8"))
            self.assertEqual([m["model"] for m in data["models"]],
                             ["llama3.1:8b", "qwen2.5:7b"])

    def test_single_model_lists_flaky_cases(self):
        spawn = FakeSpawn({"llama3.1:8b": ([[r("a", True), r("b", False)]], {})})
        with tempfile.TemporaryDirectory() as d:
            doc = Path(d) / "doc.md"
            self.assertEqual(mb.bakeoff(["llama3.1:8b"], 1, probe=FakeOllama(),
                                        spawn=spawn, doc=doc, raw_dir=Path(d)), 0)
            text = doc.read_text(encoding="utf-8")
            self.assertIn("nothing to diff", text)
            self.assertIn("- `b` (0/1)", text)

    def test_worker_that_writes_nothing_fails(self):
        with tempfile.TemporaryDirectory() as d:
            rc = mb.bakeoff(["llama3.1:8b"], 1, probe=FakeOllama(),
                            spawn=lambda m, k, out: 1, doc=Path(d) / "doc.md",
                            raw_dir=Path(d))
        self.assertEqual(rc, 1)

    def test_rounds_must_be_positive(self):
        with self.assertRaises(SystemExit):
            mb.main(["--models", "llama3.1:8b", "--rounds", "0"])


if __name__ == "__main__":
    unittest.main()
