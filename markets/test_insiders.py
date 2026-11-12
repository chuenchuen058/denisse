"""python -m unittest markets.test_insiders -v

Insiders buying (SEC Form 4): recorded EDGAR fixtures in markets/fixtures, a fake fetch,
a temp cache. No network: a test that reached urllib would fail under run_checks."""
import copy
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from markets import core, insiders

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 11, 8, 12, 0, 0)


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


class FakeEdgar:
    """Serves the recorded responses by URL and records what was asked, and with which agent."""

    def __init__(self, cfg):
        s = cfg["insiders"]
        base = s["filing_url"].format(cik=2488, accession="{acc}", document="{doc}")
        self.routes = {
            s["ticker_map_url"]: fixture("company_tickers.json"),
            s["submissions_url"].format(cik="0000002488"): fixture("submissions_amd.json"),
            s["submissions_url"].format(cik="0001045810"): b'{"filings": {"recent": {}}}',
            s["submissions_url"].format(cik="0001067983"): b'{"filings": {"recent": {}}}',
            base.format(acc="000000248826000101", doc="wk-form4_fund.xml"): fixture("form4_ten_percent_owner_purchase.xml"),
            base.format(acc="000000248826000099", doc="wk-form4_cfo.xml"): fixture("form4_officer_purchase.xml"),
        }
        self.calls = []

    def __call__(self, url, agent):
        self.calls.append((url, agent))
        if url not in self.routes:
            raise OSError(f"404 {url}")
        return self.routes[url]


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class InsidersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="markets-insiders-"))
        self.cfg = copy.deepcopy(core.load_config(core.DEFAULTS_PATH))
        self.cfg["insiders"]["cache_path"] = str(self.tmp / "cache.json")
        self.cfg["watchlist"]["edgar_user_agent"] = "Test Person test@example.com"
        self.export = (FIX / "watchlist_export.txt").read_text(encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def refresh(self, fake=None, **kw):
        clock = Clock()
        fake = fake or FakeEdgar(self.cfg)
        cache = insiders.refresh(self.cfg, fetch=fake, sleep=clock.sleep, clock=clock,
                                 export_text=self.export, now=NOW, **kw)
        return cache, fake, clock

    def test_only_open_market_purchases_by_officers_and_directors(self):
        rows = insiders.parse_form4(fixture("form4_officer_purchase.xml"), self.cfg)
        self.assertEqual(len(rows), 1)  # the grant (A), the option exercise (M) and the sale (S) are left out
        r = rows[0]
        self.assertEqual((r["ticker"], r["who"], r["date"], r["code"]), ("AMD", "Example Jane Q", "2026-11-03", "P"))
        self.assertIn("Chief Financial Officer", r["role"])
        self.assertIn(self.cfg["insiders"]["role_words"]["director"], r["role"])
        self.assertEqual((r["shares"], r["price"], r["value"]), (2000.0, 150.25, 300500.0))

    def test_ten_percent_owner_alone_is_not_an_insider_here(self):
        self.assertEqual(insiders.parse_form4(fixture("form4_ten_percent_owner_purchase.xml"), self.cfg), [])

    def test_codes_come_from_config(self):
        self.cfg["insiders"]["transaction_codes"] = ["S"]
        self.cfg["insiders"]["acquired_disposed"] = ["D"]
        rows = insiders.parse_form4(fixture("form4_officer_purchase.xml"), self.cfg)
        self.assertEqual([r["code"] for r in rows], ["S"])

    def test_bad_xml_gives_no_rows(self):
        self.assertEqual(insiders.parse_form4(b"<not-xml", self.cfg), [])

    def test_us_symbols_cover_every_section_and_skip_non_us(self):
        self.assertEqual(insiders.us_symbols(self.cfg, self.export), ["NVDA", "AMD", "BRK-B"])

    def test_us_symbols_fall_back_to_config_groups(self):
        got = insiders.us_symbols(self.cfg, "")
        self.assertIn("NVDA", got)
        self.assertNotIn("ASML", got)  # AMS: is not a US exchange
        self.assertFalse(any(s.startswith("^") for s in got))

    def test_no_user_agent_means_no_requests_and_the_panel_says_so(self):
        self.cfg["watchlist"]["edgar_user_agent"] = ""
        fake = FakeEdgar(self.cfg)
        self.refresh(fake)
        self.assertEqual(fake.calls, [])
        p = insiders.panel(self.cfg)
        self.assertEqual(p["message"], self.cfg["insiders"]["words"]["no_user_agent"])
        self.assertEqual(p["rows"], [])
        self.assertEqual(insiders.weekly_line(self.cfg, now=NOW), self.cfg["insiders"]["words"]["weekly_off"])

    def test_refresh_fetches_parses_caches_and_respects_rate_limit(self):
        cache, fake, clock = self.refresh()
        self.assertTrue(all(agent == "Test Person test@example.com" for _, agent in fake.calls))
        urls = [u for u, _ in fake.calls]
        self.assertEqual(urls[0], self.cfg["insiders"]["ticker_map_url"])
        self.assertFalse(any("8k" in u or "wk-form4_old" in u for u in urls))  # not a Form 4 / outside lookback
        self.assertFalse(any("xslF345" in u for u in urls))  # the raw XML, not the rendered page
        gap = self.cfg["insiders"]["min_request_interval_s"]
        self.assertEqual(len(clock.slept), len(fake.calls) - 1)
        self.assertTrue(all(abs(s - gap) < 1e-9 for s in clock.slept))
        self.assertEqual(cache["errors"], [])
        self.assertTrue(Path(self.cfg["insiders"]["cache_path"]).is_file())
        p = insiders.panel(self.cfg)
        self.assertEqual([(r["ticker"], r["who"]) for r in p["rows"]], [("AMD", "Example Jane Q")])
        self.assertEqual(p["message"], None)
        # second run: filings already cached are not fetched again
        _, fake2, _ = self.refresh()
        self.assertFalse(any("wk-form4" in u for u, _ in fake2.calls))

    def test_failed_requests_are_reported_not_raised(self):
        fake = FakeEdgar(self.cfg)
        del fake.routes[self.cfg["insiders"]["submissions_url"].format(cik="0000002488")]
        cache, _, _ = self.refresh(fake)
        self.assertEqual(len(cache["errors"]), 1)
        self.assertIn("1", insiders.panel(self.cfg)["message"])

    def test_request_budget_stops_the_run(self):
        self.cfg["insiders"]["max_requests_per_run"] = 1
        cache, fake, _ = self.refresh()
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(cache["errors"], [self.cfg["insiders"]["words"]["budget"]])

    def test_threshold_and_newest_first(self):
        self.refresh()
        self.cfg["insiders"]["min_value_usd"] = 300501
        self.assertEqual(insiders.panel(self.cfg)["rows"], [])
        self.cfg["insiders"]["min_value_usd"] = 0
        cache = insiders.load_cache(self.cfg["insiders"])
        cache["filings"]["x"] = {"ticker": "NVDA", "filed": "2026-11-07",
                                 "rows": [{"ticker": "NVDA", "who": "B", "role": "Director", "date": "2026-11-06",
                                           "shares": 10.0, "price": 100.0, "value": 1000.0}]}
        insiders.save_cache(self.cfg["insiders"], cache)
        self.assertEqual([r["ticker"] for r in insiders.panel(self.cfg)["rows"]], ["NVDA", "AMD"])

    def test_weekly_line_and_snapshot_carry_the_list(self):
        self.refresh()
        line = insiders.weekly_line(self.cfg, now=NOW)
        self.assertIn("AMD", line)
        self.assertIn("300,500", line)
        note = core.render_weekly_note({"tickers": []}, self.cfg)
        self.assertIn("## " + self.cfg["insiders"]["words"]["weekly_heading"], note)
        snap = core.build_snapshot_data(self.cfg)
        self.assertEqual(snap["insiders"]["heading"], self.cfg["insiders"]["words"]["heading"])

    def test_wording_is_information_only(self):
        text = " ".join(str(v) for v in self.cfg["insiders"]["words"].values()).lower()
        for word in ("should", "sell", " hold", "recommend", "signal"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
