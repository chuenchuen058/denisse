# markets

Watchlist snapshot, move alerts, weekly vault note, 13F big investors, and
**Insiders buying** (SEC Form 4). Information only: nothing here says buy,
sell or hold.

```
.venv\Scripts\python.exe -m markets snapshot            # refresh insiders (if enabled) + write .review/hub/markets.json
.venv\Scripts\python.exe -m markets snapshot --dry-run  # print, no network, no writes
.venv\Scripts\python.exe -m markets insiders [--cached] # fetch Form 4s (or show the cache) as JSON
.venv\Scripts\python.exe -m markets weekly [--dry-run]  # weekly vault note
```

Config: `config_defaults.json` holds every default; `config.json` overrides
(deep-merged). Keys starting with `_` are notes.

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
