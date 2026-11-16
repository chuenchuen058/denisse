"""python -m unittest markets.test_funds -v

Who's trading what (13F-HR, markets/funds.py): a made-up fund's two quarters in the real SEC
shapes (markets/fixtures/funds_*), a fake fetch, a temp cache and hub file. No network: a test
that reached urllib would fail under run_checks."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from markets import core, funds

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 11, 15, 12, 0, 0)
CIK = "0009999901"
AGENT = "Test Person test@example.com"


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


class FakeEdgar:
    """Serves the recorded responses by URL and records what was asked, and with which agent."""

    def __init__(self, cfg):
        s = funds.settings(cfg)
        q3 = dict(cik=int(CIK), accession="000999990126000005")
        q2 = dict(cik=int(CIK), accession="000999990126000003")
        self.routes = {
            s["ticker_map_url"]: fixture("funds_company_tickers.json"),
            s["submissions_url"].format(cik=CIK): fixture("funds_submissions.json"),
            s["index_url"].format(**q3): fixture("funds_index_q3.json"),
            s["index_url"].format(**q2): fixture("funds_index_q2.json"),
            s["filing_url"].format(document="example_q3_infotable.xml", **q3): fixture("funds_infotable_q3.xml"),
            s["filing_url"].format(document="41077.xml", **q2): fixture("funds_infotable_q2.xml"),
            s["filing_url"].format(document="primary_doc.xml", **q3): fixture("funds_primary_doc.xml"),
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


class FundsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="markets-funds-"))
        self.cfg = copy.deepcopy(core.load_config(core.DEFAULTS_PATH))
        self.cfg["funds"]["cache_path"] = str(self.tmp / "funds_cache.json")
        self.cfg["funds"]["hub_path"] = str(self.tmp / "hub" / "funds.json")
        self.cfg["insiders"]["cache_path"] = str(self.tmp / "insiders_cache.json")
        self.cfg["funds"]["list"] = [{"name": "Example Fund", "manager": "A Manager", "cik": CIK}]
        self.cfg["watchlist"]["edgar_user_agent"] = AGENT
        self.cfg["watchlist"]["tradingview_watchlist_glob"] = str(self.tmp / "*TradingView*.txt")  # never the repo

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def refresh(self, fake=None, **kw):
        clock = Clock()
        fake = fake or FakeEdgar(self.cfg)
        cache = funds.refresh(self.cfg, fetch=fake, sleep=clock.sleep, clock=clock, now=NOW, **kw)
        return cache, fake, clock

    def fund(self, watch=("MLM", "NVDA")):
        return funds.summary(self.cfg, watchlist=list(watch))["funds"][0]

    def moves(self, **kw):
        return {m["key"]: m for m in self.fund(**kw)["moves"]}


class ParseAndDiffTest(FundsBase):
    def table(self, name):
        return funds.parse_info_table(fixture(name), self.cfg)

    def diff(self):
        return funds.diff(self.table("funds_infotable_q2.xml"), self.table("funds_infotable_q3.xml"), self.cfg)

    def test_rows_are_summed_per_cusip_and_put_call(self):
        q3 = {r["key"]: r for r in self.table("funds_infotable_q3.xml")}
        self.assertEqual((q3["02079K305"]["shares"], q3["02079K305"]["value"]), (4000000.0, 800000000.0))
        self.assertEqual(q3["615369105/put"]["value"], 40000000.0)  # the put is its own position
        self.assertEqual(q3["615369105"]["shares"], 10000000.0)
        self.assertIsNone(funds.parse_info_table(fixture("funds_primary_doc.xml"), self.cfg))
        self.assertIsNone(funds.parse_info_table(b"<not-xml", self.cfg))

    def test_two_quarter_diff_new_exit_add_trim(self):
        got = {m["key"]: m for m in self.diff()}
        kinds = {k: m["kind"] for k, m in got.items()}
        self.assertEqual(kinds["573284106"], "new")
        self.assertEqual(kinds["929160109"], "new")
        self.assertEqual(kinds["13646K108"], "exit")
        self.assertEqual(kinds["369604301"], "add")
        self.assertEqual(kinds["02079K305"], "trim")
        self.assertEqual(kinds["615369105/put"], "new")
        self.assertNotIn("615369105", kinds)  # same share count: not a move
        self.assertEqual(got["573284106"]["value_change"], 758000000.0)
        self.assertEqual(got["13646K108"]["value_change"], -1500000000.0)
        self.assertEqual(got["369604301"]["change_pct"], 12.0)
        self.assertAlmostEqual(got["369604301"]["value_change"], 62400000.0)
        self.assertEqual(got["02079K305"]["change_pct"], -20.0)
        self.assertEqual(self.diff()[0]["kind"], "exit")  # biggest money first: the $1.5 Bill exit
        self.assertEqual(funds.diff(None, self.table("funds_infotable_q3.xml"), self.cfg), [])

    def test_thresholds_come_from_config(self):
        keys = lambda: {m["key"] for m in self.diff()}
        self.assertNotIn("92826C839", keys())    # Visa +3.3% shares: under move_min_pct
        self.assertNotIn("99999AA11", keys())    # +50% shares but $5M: under move_min_usd
        self.cfg["funds"]["move_min_usd"] = 1000000
        self.assertIn("99999AA11", keys())
        self.cfg["funds"]["move_min_pct"] = 3
        self.assertIn("92826C839", keys())
        self.cfg["funds"]["move_min_pct"] = 13
        self.assertNotIn("369604301", keys())    # GE +12% drops out
        self.cfg["funds"]["new_exit_min_usd"] = 60000000
        self.assertNotIn("98877X101", keys())    # the $50 Mil new position drops out
        self.assertNotIn("615369105/put", keys())

    def test_quarter_label(self):
        self.assertEqual(funds.quarter("2026-09-30", self.cfg), "Q3 2026")
        self.assertEqual(funds.quarter("2026-03-31", self.cfg), "Q1 2026")
        self.assertEqual(funds.quarter("", self.cfg), "")


class RefreshTest(FundsBase):
    def test_no_user_agent_means_no_requests_and_it_says_so(self):
        self.cfg["watchlist"]["edgar_user_agent"] = ""
        _, fake, _ = self.refresh()
        self.assertEqual(fake.calls, [])
        w = self.cfg["funds"]["words"]
        self.assertEqual(funds.summary(self.cfg, watchlist=[])["message"], w["no_user_agent"])
        self.assertEqual(funds.weekly_section(self.cfg, now=NOW), [w["weekly_off"]])
        self.assertIsNone(funds.hub_payload(self.cfg)["funds"][0]["latest"])

    def test_disabled_means_no_requests(self):
        self.cfg["funds"]["enabled"] = False
        _, fake, _ = self.refresh()
        self.assertEqual(fake.calls, [])

    def test_refresh_reads_the_two_newest_13f_hr_and_respects_the_rate_limit(self):
        cache, fake, clock = self.refresh()
        self.assertEqual(cache["errors"], [])
        self.assertTrue(all(agent == AGENT for _, agent in fake.calls))
        urls = [u for u, _ in fake.calls]
        for skipped in ("000999990126000001", "000999990126000002", "000999990126000004"):
            self.assertFalse(any(skipped in u for u in urls), skipped)  # older Q1, the amendment, the SC 13G
        self.assertFalse(any(u.endswith("primary_doc.xml") for u in urls))  # never the cover page
        gap = funds.settings(self.cfg)["min_request_interval_s"]
        self.assertEqual(len(clock.slept), len(fake.calls))  # a gap before every request, the first too
        self.assertTrue(all(abs(s - gap) < 1e-9 for s in clock.slept))
        f = self.fund()
        self.assertEqual(f["latest"]["filed"], "2026-11-14")
        self.assertEqual(f["latest"]["quarter"], "Q3 2026")
        self.assertEqual(f["previous"]["quarter"], "Q2 2026")
        # second run: filings already cached are not fetched again
        _, fake2, _ = self.refresh()
        self.assertFalse(any("infotable" in u or "41077" in u for u, _ in fake2.calls))

    def test_refresh_hours_skips_a_recent_check_unless_forced(self):
        self.refresh()
        _, fake, _ = self.refresh(force=False)
        self.assertEqual(fake.calls, [])
        _, fake, _ = self.refresh(force=True)
        self.assertGreater(len(fake.calls), 0)

    def test_failures_are_reported_not_raised(self):
        fake = FakeEdgar(self.cfg)
        del fake.routes[funds.settings(self.cfg)["submissions_url"].format(cik=CIK)]
        cache, _, _ = self.refresh(fake)
        self.assertEqual(len(cache["errors"]), 1)
        self.assertIn("Example Fund", cache["errors"][0])
        self.assertIn("1", funds.summary(self.cfg, watchlist=[])["message"])

    def test_request_budget_stops_the_run(self):
        self.cfg["funds"]["max_requests_per_run"] = 2
        cache, fake, _ = self.refresh()
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(cache["errors"], [self.cfg["funds"]["words"]["budget"]])


class SummaryTest(FundsBase):
    def setUp(self):
        super().setUp()
        self.refresh()

    def test_tickers_from_names_unknown_cusip_and_share_classes(self):
        m = self.moves()
        self.assertEqual(m["573284106"]["ticker"], "MLM")      # MATLS spelt out
        self.assertEqual(m["929160109"]["ticker"], "VMC")
        self.assertEqual(m["615369105/put"]["ticker"], "MCO")  # "/DE/" dropped
        self.assertIsNone(m["98877X101"]["ticker"])            # unknown: the issuer name is shown
        self.assertEqual(funds.who(m["98877X101"]), "ZEBRA OBSCURE HLDGS")
        self.assertIsNone(m["02079K305"]["ticker"])            # GOOGL and GOOG: ambiguous, name shown
        self.assertEqual(funds.who(m["02079K305"]), "ALPHABET INC")
        self.cfg["funds"]["cusip_tickers"] = {"02079K305": "GOOGL"}
        self.assertEqual(self.moves()["02079K305"]["ticker"], "GOOGL")

    def test_top_positions_with_share_of_portfolio(self):
        f = self.fund()
        self.assertEqual(len(f["top"]), self.cfg["funds"]["top_positions"])
        self.assertEqual(f["top"][0]["ticker"], "MCO")
        total = 4100 + 758 + 722 + 800 + 1050 + 582.4 + 15 + 50 + 40
        self.assertAlmostEqual(f["top"][0]["pct_of_portfolio"], round(100 * 4100 / total, 2))
        self.assertEqual(f["positions"], 9)

    def test_watchlist_moves_are_flagged(self):
        f = self.fund(watch=["MLM"])
        self.assertEqual([m["ticker"] for m in f["watch"]], ["MLM"])
        data = funds.summary(self.cfg, watchlist=["MLM"])
        self.assertEqual(data["watch"][0]["fund"], "Example Fund")
        self.assertEqual(data["watch"][0]["filed"], "2026-11-14")
        self.assertEqual(self.fund(watch=[])["watch"], [])

    def test_price_a_share(self):
        self.assertEqual(funds.price(583.0812, self.cfg), "$583.08")
        self.assertEqual(funds.price(None, self.cfg), "?")
        self.assertEqual(funds._per_share({"value": 100.0, "shares": 4.0}), 25.0)
        self.assertIsNone(funds._per_share({"value": 100.0, "shares": 0}))

    def test_wording_money_and_cli_text(self):
        m = self.moves()
        self.assertEqual(funds.move_text(m["573284106"], self.cfg).split(" · ")[:2], ["New position", "$758 Mil at about $583.08 a share"])
        self.assertIn("$1.5 Bill", funds.move_text(m["13646K108"], self.cfg))
        self.assertIn("12%", funds.move_text(m["369604301"], self.cfg))
        self.assertIn("put", funds.move_text(m["615369105/put"], self.cfg))
        self.assertEqual(funds.money(62400000, self.cfg), "$62.4 Mil")
        text = funds.text_summary(self.cfg)
        for needle in ("Example Fund", "Q3 2026", "2026-11-14", self.cfg["funds"]["words"]["small_print"]):
            self.assertIn(needle, text)

    def test_hub_file_carries_funds_moves_and_insiders(self):
        path = funds.write_hub(self.cfg)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        f = data["funds"][0]
        self.assertEqual(len(f["card_moves"]), self.cfg["funds"]["card_moves"])
        self.assertTrue(all(m["text"] and m["label"] for m in f["card_moves"]))
        self.assertEqual(data["heading"], self.cfg["funds"]["words"]["heading"])
        self.assertEqual(data["small_print"], self.cfg["funds"]["words"]["small_print"])
        self.assertEqual(data["insiders"]["heading"], self.cfg["insiders"]["words"]["heading"])

    def test_weekly_section_per_fund_that_filed_this_week(self):
        lines = funds.weekly_section(self.cfg, now=NOW)
        self.assertTrue(lines[0].startswith("### Example Fund"))
        self.assertIn("Q3 2026", lines[0])
        self.assertTrue(any("MLM" in l for l in lines))
        later = datetime(2026, 12, 1)
        self.assertEqual(funds.weekly_section(self.cfg, now=later), [self.cfg["funds"]["words"]["weekly_none"]])

    def test_weekly_note_has_the_section(self):
        note = core.render_weekly_note({"tickers": []}, self.cfg)
        self.assertIn("## " + self.cfg["funds"]["words"]["weekly_heading"], note)  # its lines: the test above (fixed clock)

    def test_wording_is_information_only(self):
        text = json.dumps(self.cfg["funds"]["words"]).lower()
        for word in ("should", "sell", " hold", "recommend", "signal", "buy"):
            self.assertNotIn(word, text)


class ConfigTest(unittest.TestCase):
    def test_every_fund_has_a_cik_and_the_committed_agent_is_empty(self):
        cfg = core.load_config(core.DEFAULTS_PATH)
        self.assertGreaterEqual(len(funds.fund_list(cfg)), 1)
        self.assertTrue(all(len(f["cik"]) == 10 and f["cik"].isdigit() for f in funds.fund_list(cfg)))
        for p in (core.DEFAULTS_PATH, core.CONFIG_PATH):
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["watchlist"]["edgar_user_agent"], "")

    def test_local_file_and_environment_supply_the_agent(self):
        tmp = Path(tempfile.mkdtemp(prefix="markets-local-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        local = tmp / "markets.local.json"
        local.write_text(json.dumps({"watchlist": {"edgar_user_agent": "Local Person local@example.com"}}),
                         encoding="utf-8")
        cfg_file = tmp / "config.json"
        cfg_file.write_text(json.dumps({"local_config_path": str(local)}), encoding="utf-8")  # absolute wins over the repo
        env_name = core.load_config(core.DEFAULTS_PATH)["user_agent_env"]
        with mock.patch.object(core, "CONFIG_PATH", cfg_file), mock.patch.dict(os.environ, {env_name: ""}):
            self.assertEqual(core.load_config()["watchlist"]["edgar_user_agent"], "Local Person local@example.com")
            self.assertEqual(core.load_config(cfg_file)["watchlist"]["edgar_user_agent"], "")  # explicit path: no overlay
        with mock.patch.object(core, "CONFIG_PATH", cfg_file), \
                mock.patch.dict(os.environ, {env_name: "Env Person env@example.com"}):
            self.assertEqual(core.load_config()["watchlist"]["edgar_user_agent"], "Env Person env@example.com")


if __name__ == "__main__":
    unittest.main()
