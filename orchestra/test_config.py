"""Tests for the runner's config: every limit, pattern and lane command it uses comes from
config.json, a changed value changes behaviour, and a bad value falls back to
config_defaults.json with a log line instead of crashing a tick.

    .venv\\Scripts\\python.exe -m unittest orchestra.test_config -v
"""
import datetime as dt
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent))  # testing_git; appended so it never shadows a local module
import testing_git  # noqa: E402

TASK = textwrap.dedent("""\
    # t
    Queue: auto
    Priority: normal
    Weight: light
    Kind: work
    Lane: {lane}
    Written: {written}

    Do it.

    ## Gate
    ```
    {gate}
    ```
    """)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        testing_git.hermetic_git(self)  # first: no inherited GIT_* reaches a real repo
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".review").mkdir(parents=True)
        # Other test modules switch the live window and toasts off by env at import; this one
        # compares the loaded config with the defaults, so those overrides stay out of it.
        self._env = {k: os.environ.get(k) for k in ("ORCHESTRA_REPO", "ORCHESTRA_POWER",
                                                    "ORCHESTRA_LIVE_WINDOW", "ORCHESTRA_TOASTS")}
        os.environ.pop("ORCHESTRA_LIVE_WINDOW", None)
        os.environ.pop("ORCHESTRA_TOASTS", None)
        os.environ["ORCHESTRA_REPO"] = str(self.repo)
        os.environ["ORCHESTRA_POWER"] = "ac"
        import runner
        self.R = importlib.reload(runner)
        # No tick here, but never a real window or toast whatever a test reaches (orchestra/visible.py).
        quiet = mock.patch.multiple(self.R.visible, SPAWN=mock.Mock(return_value=0), RUN=mock.Mock())
        quiet.start()
        self.addCleanup(quiet.stop)
        self.shipped = dict(self.R.C)
        self.defaults = {k: v for k, v in json.loads(self.R.CONFIG_DEFAULTS.read_text(encoding="utf-8")).items()
                         if not k.startswith("_")}
        self.logged = []

    def tearDown(self):
        self.R.C.clear()
        self.R.C.update(self.shipped)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use(self, text_or_changes) -> dict:
        """Load a temp config.json (the shipped one with `changes`, or raw text) into runner.C."""
        p = self.tmp / "config.json"
        if isinstance(text_or_changes, str):
            p.write_text(text_or_changes, encoding="utf-8")
        else:
            base = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
            p.write_text(json.dumps({**base, **text_or_changes}), encoding="utf-8")
        cfg = self.R.load_config(log_fn=self.logged.append, path=p)
        self.R.C.clear()
        self.R.C.update(cfg)
        return cfg

    def task(self, lane="claude", gate=f'"{sys.executable}" -c "pass"'):
        p = self.tmp / f"t-{lane}.md"
        p.write_text(TASK.format(lane=lane, gate=gate, written=dt.datetime.now().strftime("%Y-%m-%dT%H:%M")),
                     encoding="utf-8")
        return self.R.parse_task(p)

    def lane_log(self, *lines):
        p = self.tmp / "x.lane.log"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    # --- shipped values

    def test_defaults_match_shipped_config(self):
        shipped = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        for k, v in self.defaults.items():
            with self.subTest(key=k):
                self.assertIn(k, shipped, f"{k} is in config_defaults.json but not config.json")
                self.assertEqual(shipped[k], v, f"{k}: config.json and config_defaults.json disagree")

    def test_shipped_config_loads_clean(self):
        self.use({})
        self.assertEqual(self.logged, [])

    def test_env_switch_reads_its_word(self):
        """ORCHESTRA_<KEY>=false is off: bool("false") would have been True."""
        for word, want in (("false", False), ("0", False), ("", False), ("no", False), ("true", True), ("1", True)):
            with self.subTest(word=word), mock.patch.dict(os.environ, {"ORCHESTRA_TOASTS": word}):
                self.assertIs(self.use({"toasts": not want})["toasts"], want)

    # --- each moved value changes behaviour

    def test_limit_patterns(self):
        log = self.lane_log("Custom quota wall hit")
        self.use({})
        self.assertIsNone(self.R.detect_limit("claude", log))
        self.use({"limit_patterns": {"claude": ["custom quota wall"]}})
        self.assertEqual(self.R.detect_limit("claude", log), "Custom quota wall hit")
        self.assertIsNone(self.R.detect_limit("claude", self.lane_log("usage limit reached")))

    def test_limit_tail_lines(self):
        log = self.lane_log("usage limit reached", *["working"] * 5)
        self.use({})
        self.assertIsNotNone(self.R.detect_limit("claude", log))
        self.use({"limit_tail_lines": 3})
        self.assertIsNone(self.R.detect_limit("claude", log))

    def test_limit_default_min(self):
        for mins in (60, 7):
            self.use({} if mins == 60 else {"limit_default_min": mins})
            state = self.R.load_state()
            before = self.R.now()
            self.R._limited(self.task(), state, {"attempts": 1, "history": []}, "usage limit reached", 1,
                            "", provenance={"model": "x"})
            until = dt.datetime.fromisoformat(state["lanes"]["claude"]["limited_until"])
            self.assertAlmostEqual((until - before).total_seconds(), mins * 60, delta=5)

    def test_lane_argv(self):
        self.use({})
        self.assertEqual(self.R.lanes()["claude"][0], "claude")
        self.use({"lane_argv": {"mine": ["mycli", "--go"], "gone": ["node", "{npm:@nobody/not-installed:x}"],
                                "shim": ["{npm-shim:surely-no-such-cli}", "run"]}})
        lanes = self.R.lanes()
        self.assertEqual(lanes["mine"], ["mycli", "--go"])
        self.assertNotIn("gone", lanes)  # npm CLI not installed: the lane is left out
        self.assertNotIn("claude", lanes)
        self.assertEqual(lanes["shim"], ["surely-no-such-cli", "run"])  # no shim found: bare name

    def test_unpriced_lanes(self):
        state = self.R.load_state()
        self.R.today_usage(state)["cost_usd"] = 1e6  # over any cap
        self.use({"lane_argv": {"cheap": ["x"]}})
        self.assertIn("spend cap", self.R.why_not_runnable(self.task("cheap"), state))
        self.use({"lane_argv": {"cheap": ["x"]}, "unpriced_lanes": ["cheap"]})
        self.assertIsNone(self.R.why_not_runnable(self.task("cheap"), state))

    def test_broken_gate_codes(self):
        t = self.task(gate=f'"{sys.executable}" -c "raise SystemExit(3)"')
        self.use({})
        self.assertEqual(self.R.run_gate(t, self.tmp)[0], "fail")
        self.use({"broken_gate_codes": [3]})
        self.assertEqual(self.R.run_gate(t, self.tmp)[0], "broken")

    def _git_repo_with_branch(self, files: dict) -> str:
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        g("init", "-q", "-b", "main")
        g("config", "user.name", "t"); g("config", "user.email", "t@t")
        (self.repo / "README").write_text("x\n")
        g("add", "README"); g("commit", "-qm", "init")
        g("checkout", "-qb", "review/x")
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
            g("add", rel)
        g("commit", "-qm", "change")
        g("checkout", "-q", "main")
        return "review/x"

    def test_review_risky_patterns_and_max_lines(self):
        branch = self._git_repo_with_branch({"orchestra/a.py": "1\n2\n3\n", "docs/b.md": "1\n"})
        self.use({})
        self.assertIn("orchestra/a.py", self.R.risk(branch))
        self.use({"review_risky_patterns": ["^docs/"]})
        why = self.R.risk(branch)
        self.assertIn("docs/b.md", why)
        self.assertNotIn("orchestra/a.py", why)
        self.use({"review_risky_patterns": [], "review_max_lines": 3})
        self.assertIn("4 changed lines (over 3)", self.R.risk(branch))
        self.use({"review_risky_patterns": []})
        self.assertIsNone(self.R.risk(branch))

    def _fake_reviewer(self, sleep=0.0):
        out = self.tmp / "reviewer_prompt.txt"
        code = (f"import sys,time; open(r'{out}','w',encoding='utf-8').write(sys.stdin.read()); "
                f"time.sleep({sleep}); print('VERDICT: ok fine')")
        return [sys.executable, "-c", code], out

    def test_reviewer_argv_and_diff_cut(self):
        branch = self._git_repo_with_branch({"big.txt": "line\n" * 200})
        (self.repo / ".review" / "reports").mkdir(parents=True, exist_ok=True)
        argv, out = self._fake_reviewer()
        report = self.tmp / "r.md"
        report.write_text("RESULT: PASS\n")
        self.use({"reviewer_argv": argv, "review_diff_max_chars": 50})
        rv = self.R.run_reviewer(self.task(), self.repo, branch, report, "test")
        self.assertEqual(rv["verdict"], "ok")
        self.assertIn("diff cut at 50 chars", out.read_text(encoding="utf-8"))
        self.use({"reviewer_argv": ["surely-no-such-reviewer-cli"]})
        rv = self.R.run_reviewer(self.task(), self.repo, branch, report, "test")
        self.assertEqual(rv["verdict"], "block")
        self.assertIn("surely-no-such-reviewer-cli", rv["reasons"])

    def test_review_timeout_s(self):
        branch = self._git_repo_with_branch({"a.txt": "1\n"})
        (self.repo / ".review" / "reports").mkdir(parents=True, exist_ok=True)
        argv, _ = self._fake_reviewer(sleep=5)
        report = self.tmp / "r.md"
        report.write_text("RESULT: PASS\n")
        self.use({"reviewer_argv": argv, "review_timeout_s": 1})
        rv = self.R.run_reviewer(self.task(), self.repo, branch, report, "test")
        self.assertEqual(rv["verdict"], "block")
        self.assertIn("timed out after 1s", rv["reasons"])

    # --- bad values fall back, with a log line

    def test_unreadable_config_falls_back_wholesale(self):
        cfg = self.use("{ not json")
        for k, v in self.defaults.items():
            self.assertEqual(cfg[k], v)
        self.assertTrue(any("unreadable" in m for m in self.logged), self.logged)

    def test_non_object_config_falls_back(self):
        cfg = self.use("[1, 2]")
        self.assertEqual(cfg["infra_retries"], self.defaults["infra_retries"])
        self.assertTrue(any("not a JSON object" in m for m in self.logged), self.logged)

    def test_wrong_shapes_fall_back_per_key(self):
        cfg = self.use({
            "limit_default_min": "soon",                       # not a number
            "limit_tail_lines": -1,                            # negative
            "parallel_lanes": "yes",                           # not a bool
            "limit_patterns": {"claude": ["(unclosed"]},       # bad regex
            "lane_argv": {"claude": []},                       # empty command
            "unpriced_lanes": [1, 2],                          # wrong item type
            "timeout_s": {"light": 10},                        # a weight missing
            "broken_gate_codes": "5",                          # not a list
            "review_max_lines": 99,                            # fine: kept
            "something_new": "kept",                           # unknown keys pass through
        })
        for k in ("limit_default_min", "limit_tail_lines", "parallel_lanes", "limit_patterns", "lane_argv",
                  "unpriced_lanes", "timeout_s", "broken_gate_codes"):
            with self.subTest(key=k):
                self.assertEqual(cfg[k], self.defaults[k])
                self.assertTrue(any(m.startswith(f"config: {k} ") for m in self.logged), self.logged)
        self.assertEqual(cfg["review_max_lines"], 99)
        self.assertEqual(cfg["something_new"], "kept")
        self.assertFalse(any("review_max_lines" in m for m in self.logged))

    def test_missing_keys_get_defaults_silently(self):
        cfg = self.use("{}")
        for k, v in self.defaults.items():
            self.assertEqual(cfg[k], v)
        self.assertEqual(self.logged, [])

    def test_bad_value_does_not_crash_a_tick(self):
        self.use({"limit_patterns": "nope", "lane_argv": None, "review_risky_patterns": ["["]})
        self.assertIsNone(self.R.detect_limit("claude", self.lane_log("all good")))
        self.assertIn("claude", self.R.lanes())
        self.assertEqual(self.R.C["review_risky_patterns"], self.defaults["review_risky_patterns"])

    def test_bad_env_override_is_ignored(self):
        os.environ["ORCHESTRA_LIMIT_DEFAULT_MIN"] = "not-a-number"
        try:
            cfg = self.use({})
        finally:
            os.environ.pop("ORCHESTRA_LIMIT_DEFAULT_MIN", None)
        self.assertEqual(cfg["limit_default_min"], self.defaults["limit_default_min"])
        self.assertTrue(any("ORCHESTRA_LIMIT_DEFAULT_MIN" in m for m in self.logged), self.logged)


if __name__ == "__main__":
    unittest.main()
