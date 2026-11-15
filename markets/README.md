# markets

Watchlist snapshot, move alerts, weekly vault note, 13F big investors, and
**Insiders buying** (SEC Form 4). Information only: nothing here says buy,
sell or hold.

```
.venv\Scripts\python.exe -m markets snapshot            # refresh insiders (if enabled) + write .review/hub/markets.json
.venv\Scripts\python.exe -m markets snapshot --dry-run  # print, no network, no writes
.venv\Scripts\python.exe -m markets insiders [--cached] # fetch Form 4s (or show the cache) as JSON
.venv\Scripts\python.exe -m markets weekly [--dry-run]  # weekly vault note
.venv\Scripts\python.exe -m markets funds [--cached|--dry-run]  # big funds' 13F moves: print + .review/hub/funds.json
```

Config: `config_defaults.json` holds every default; `config.json` overrides
(deep-merged). Keys starting with `_` are notes.

## The SEC contact stays out of the repo

SEC EDGAR asks every request to carry a User-Agent with a name and a contact email.
This repo is public, so `watchlist.edgar_user_agent` stays `""` in the committed config.
The real value goes in one of these places (both are read only by the live config, i.e.
`load_config()` with no path):

- `.review/markets.local.json` (git-ignored; `local_config_path`), deep-merged over the
  config, e.g. `{"watchlist": {"edgar_user_agent": "Your Name you@example.com"}}`;
- the environment variable named by `user_agent_env` (`DENISSE_EDGAR_USER_AGENT`), which
  wins over both.

With neither set, nothing is fetched (insiders and funds) and the panels say so.

## Who's trading what (`funds` block)

Big funds' 13F-HR filings, quarter on quarter. For each fund in `funds.list` (name,
manager, SEC CIK of the 13F filer) the two newest 13F-HR filings for different quarters
are read from EDGAR (the submissions JSON, the filing folder's `index.json`, its
information-table XML; each filing once, cached) and compared: new positions, exits,
and adds/trims over the thresholds, plus the largest positions with their share of the
portfolio. Positions are summed per CUSIP (puts and calls separately). Tickers come from
matching the issuer name to sec.gov `company_tickers.json` titles (one match only;
share classes such as GOOGL/GOOG show the issuer name), or `cusip_tickers`.

13F is filed up to 45 days after the quarter ends: every row says the quarter it covers
and the date it was filed. It shows US-listed long positions and listed options only.
Information only: no buy/sell/hold wording anywhere.

Where it shows: `python -m markets funds` prints it and writes `.review/hub/funds.json`
(also written by `snapshot`, which refreshes at most every `refresh_hours`); the Atrium
hub's "Who's trading what" card (`atrium/config.json` → `live.board.funds`) shows moves in
watchlist tickers first, each fund's newest filing and top moves, then the Form 4 insiders
list; the weekly note has one `###` section per fund that filed in the last `weekly_days`.

The request settings it does not set itself (`min_request_interval_s`,
`max_requests_per_run`, `timeout_s`, `ticker_map_url`, `ticker_map_max_age_days`,
`submissions_url`) are the `insiders` block's: one SEC fair-access policy for both.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Master switch. |
| `list` | 10 funds (TCI, Berkshire, Pershing Square, Scion, Appaloosa, Third Point, Baupost, Duquesne, Viking, Lone Pine) | `name`, `manager`, `cik`, optional `note`. CIKs checked on EDGAR 15 Nov 2026. A `list` in `config.json` replaces this one. |
| `forms` | `["13F-HR"]` | Filing types read (amendments left out). |
| `filings_per_fund` | `2` | Newest filings kept, one per quarter. |
| `index_url` / `filing_url` | sec.gov Archives | Filing folder index and a document in it. |
| `info_table_ext` / `skip_documents` / `info_row_tag` | `.xml` / `primary_doc.xml` / `infoTable` | How the information table is found in the folder. |
| `value_multiplier` | `1` | 13F `value` is whole dollars since 2023. |
| `move_min_pct` / `move_min_usd` | `10` / `25000000` | An add/trim is listed when shares moved by at least this % and the money moved (shares moved × this quarter's price) by at least this. |
| `new_exit_min_usd` | `1000000` | New positions and exits at or over this. |
| `top_positions` / `card_moves` / `cli_moves` / `weekly_moves` | `5` / `3` / `10` / `5` | How many of each are shown. |
| `weekly_days` | `7` | Window for the weekly note's fund sections. |
| `refresh_hours` | `24` | `snapshot` refreshes at most this often. |
| `cusip_tickers` | `{}` | CUSIP → ticker, wins over the name match. |
| `name_abbreviations` / `name_drop_words` | — | Name normalisation for the issuer → ticker match. |
| `cache_path` / `hub_path` | `.review/markets_funds_cache.json` / `.review/hub/funds.json` | Never tracked. |
| `words` | — | Every line of wording (CLI, card payload, weekly note). |

## Insiders buying (`insiders` block)

Executives buying their own company's stock with their own money. For every
US symbol on the watchlist (all `###` sections of the newest TradingView
export; the config `watchlist.groups` via `symbol_map` when there is no
export), recent Form 4 filings are read from SEC EDGAR and only open-market
purchases by officers and directors are kept. Grants, option exercises and
sales are left out.

Off until `watchlist.edgar_user_agent` is set (SEC requires a contact in the
User-Agent, e.g. `"Your Name you@example.com"`). With it empty, no request is
made and the panel says so.

The cache is `.review/markets_insiders_cache.json` (never tracked); each
filing is fetched once. The Atrium markets card shows the list under the
tickers (wording in `atrium/config.json` → `live.board.markets.insiders`); the
weekly note gets an "Insiders buying this week" line.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Master switch; `false` = no requests, panel says it is off. |
| `forms` | `["4", "4/A"]` | Filing form types read from the submissions index. |
| `transaction_codes` | `["P"]` | Form 4 table I codes kept. `P` = open-market/private purchase. |
| `acquired_disposed` | `["A"]` | Acquired (A) / disposed (D) codes kept; empty = any. |
| `roles` | `["officer", "director"]` | Reporting-owner flags that count. 10% owners alone do not. |
| `role_words` | Officer / Director | Label when an officer has no title, and for directors. |
| `truthy` | `["1", "true"]` | How Form 4 writes a set flag. |
| `us_exchanges` | NASDAQ, NYSE, AMEX, … | TradingView prefixes treated as US-listed. |
| `ticker_replace` | `{".": "-"}` | TradingView to SEC spelling (`BRK.B` → `BRK-B`). |
| `lookback_days` | `30` | Only filings filed this recently are fetched. |
| `keep_days` | `120` | Cached filings older than this are dropped. |
| `max_filings_per_symbol` | `20` | Newest Form 4s looked at per symbol per run. |
| `min_value_usd` | `50000` | Panel/weekly threshold on shares × price. |
| `panel_max` | `8` | Rows on the Atrium card (newest first). |
| `weekly_days` / `weekly_max` | `7` / `5` | Window and item count for the weekly note line. |
| `ticker_map_url` | sec.gov `company_tickers.json` | Ticker → CIK map. |
| `ticker_map_max_age_days` | `7` | Re-download the ticker map after this. |
| `submissions_url` | `data.sec.gov/submissions/CIK{cik}.json` | Per-company filing index; `{cik}` is 10 digits. |
| `filing_url` | `sec.gov/Archives/edgar/data/{cik}/{accession}/{document}` | The Form 4 XML. |
| `min_request_interval_s` | `0.15` | Gap between requests (SEC fair access: ≤10 per second). |
| `max_requests_per_run` | `300` | Hard cap per refresh; the rest waits for the next run. |
| `timeout_s` | `20` | Per-request timeout. |
| `cache_path` | `.review/markets_insiders_cache.json` | Cache file, relative to the repo. |
| `words` | — | Panel and weekly-note wording (heading, off/error messages, line templates). |

## Other keys

| Key | Meaning |
| --- | --- |
| `watchlist.groups` | Group name → symbols, used when there is no TradingView export. |
| `watchlist.symbol_map` | Local symbol → TradingView symbol. |
| `watchlist.tradingview_watchlist_glob` / `_files` | Where TradingView exports are found; the newest wins. |
| `watchlist.etf_holdings` | ETF → holdings URL. |
| `watchlist.followed_managers` | 13F filers to follow (name, CIK, `historical_only`, `note`). |
| `watchlist.edgar_user_agent` | Contact for SEC EDGAR requests; empty = EDGAR features off. |
| `watchlist.holdings` | Symbol → date owned since, for the weekly note. |
| `watchlist.weekly_note_day` | Review day named in the weekly note. |
| `watchlist.source_label` | Where the watchlist came from, for the weekly note. |
| `alert_move_pct` | Day move (%) that raises an alert, once per ticker per day. |
| `refresh_minutes` | Intended refresh interval. |
| `markets_path` | Snapshot file the Atrium card reads. |
| `alert_state_path` | Alert dedupe state. |
| `vault_root` | Vault folder for the weekly note. |
