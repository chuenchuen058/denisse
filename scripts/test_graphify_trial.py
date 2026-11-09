"""Tests for scripts/graphify_trial.py (lane-log re-read measurement)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graphify_trial as gt  # noqa: E402

CWD = "C:\\wt\\task"


def _assistant(mid, uses):
    return {"type": "assistant", "parent_tool_use_id": None,
            "message": {"id": mid, "usage": {"cache_read_input_tokens": 100},
                        "content": [{"type": "tool_use", "id": u, "name": n, "input": i} for u, n, i in uses]}}


def _result(uid, text):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": uid, "content": text}]}}


class GraphifyTrialTest(unittest.TestCase):
    def setUp(self):
        self.cfg = gt.load_config(gt.DEFAULT_CONFIG)

    def _log(self, events):
        d = tempfile.mkdtemp()
        p = Path(d) / "x.lane.log"
        p.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        return p

    def test_shell_read_classification_ignores_quoted_pipes(self):
        self.assertTrue(gt._shell_is_read('grep -n "a\\|b" orchestra/runner.py | head -5', self.cfg))
        self.assertTrue(gt._shell_is_read("cd x && sed -n 1,9p a.py; echo ---; cat b.md", self.cfg))
        self.assertFalse(gt._shell_is_read(".venv/Scripts/python.exe -m unittest t", self.cfg))
        self.assertFalse(gt._shell_is_read("cat > f.py <<'EOF'\nx\nEOF", self.cfg))

    def test_categories_and_weighting(self):
        events = [
            {"type": "system", "subtype": "init", "cwd": CWD},
            _assistant("m1", [("r1", "Read", {"file_path": CWD + "\\pkg\\a.py"}),
                              ("r2", "Read", {"file_path": CWD + "\\pkg\\b.py"})]),
            _result("r1", "x" * 400), _result("r2", "y" * 40),
            _assistant("m2", [("e1", "Edit", {"file_path": CWD + "\\pkg\\b.py"}),
                              ("r3", "Read", {"file_path": CWD + "\\pkg\\a.py"})]),
            _result("e1", "ok"), _result("r3", "x" * 400),
            _assistant("m3", [("b1", "Bash", {"command": "cat .review/task.md"})]),
            _result("b1", "t" * 80),
            {"type": "result", "usage": {"cache_read_input_tokens": 300}},
        ]
        r = gt.analyse(self._log(events), self.cfg)
        cats = r["by_category"]
        self.assertEqual(r["api_calls_main"], 3)
        self.assertEqual(r["rereads"], 1)
        self.assertEqual(r["edited_files"], ["pkg/b.py"])
        # a.py first read at call 0 -> carried by 2 later calls: 400/4*2
        self.assertEqual(cats["read_code_explore"]["cache_read_est"], 200)
        self.assertEqual(cats["read_edited"]["n"], 1)
        self.assertEqual(cats["reread"]["cache_read_est"], 100)
        self.assertEqual(cats["shell_read_noncode"]["cache_read_est"], 0)
        self.assertEqual(r["cache_read_summed_calls"], 300)


if __name__ == "__main__":
    unittest.main()
