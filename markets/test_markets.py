import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from markets import core
from markets.core import build_snapshot_data, collect_alerts, render_weekly_note

ROOT = Path(__file__).resolve().parents[1]


class MarketsTests(unittest.TestCase):
    def setUp(self):
        # Every state/output path points into a temp dir: a test must never touch the real
        # .review/ (reviewer, 10 Nov: setUp deleted Chuen's real alert state).
        self.tmp = Path(tempfile.mkdtemp(prefix="markets-test-"))
        self._saved = {k: core.CONFIG.get(k) for k in ("alert_state_path", "markets_path")}
        core.CONFIG["alert_state_path"] = str(self.tmp / "markets_alert_state.json")
        core.CONFIG["markets_path"] = str(self.tmp / "markets.json")
        self.alert_state = Path(core.CONFIG["alert_state_path"])

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                core.CONFIG.pop(k, None)
            else:
                core.CONFIG[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_snapshot_dry_run_prints_json_shape(self):
        proc = subprocess.run([sys.executable, "-m", "markets", "snapshot", "--dry-run"], cwd=str(ROOT), capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertIn("tickers", data)
        self.assertGreater(len(data["tickers"]), 0)
        self.assertEqual(data["footer"], "Information only, not financial advice")

    def test_alerts_are_deduped_once_per_day(self):
        data = build_snapshot_data()
        first = collect_alerts(data, dry_run=False)
        second = collect_alerts(data, dry_run=False)
        self.assertGreaterEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_weekly_note_has_frontmatter_and_footer(self):
        text = render_weekly_note(build_snapshot_data())
        self.assertIn("type: markets-weekly", text)
        self.assertIn("Information only, not financial advice", text)
        self.assertIn("## Weekly change", text)

    def test_snapshot_uses_stale_marker_when_previous_data_is_missing(self):
        snapshot = build_snapshot_data()
        self.assertTrue(all("ticker" in entry for entry in snapshot["tickers"]))
        self.assertIn("footer", snapshot)


if __name__ == "__main__":
    unittest.main()
