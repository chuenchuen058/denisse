"""invest.json: the shipped values equal invest.DEFAULTS, changing the data
changes the behaviour, and a bad value logs a warning and falls back to its
default instead of crashing.

Synthetic fixtures, temporary stores and config files, no model, no network.

    .venv\\Scripts\\python.exe scripts\\test_invest_config.py
"""
import datetime as dt
import json
import logging
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import invest  # noqa: E402

results = []
NOW = dt.datetime(2026, 10, 30, 18, 0, tzinfo=dt.timezone.utc)
M = {"type": "Equity", "timezone": "UTC+01", "region": "United Kingdom", "currency": "GBX"}
P = invest.FixtureProvider(matches={"ACME.LON": [dict(M, symbol="ACME.LON", name="Acme plc (synthetic)")]})


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = Capture()
invest.log.addHandler(cap)


def strip_notes(d):
    if isinstance(d, dict):
        return {k: strip_notes(v) for k, v in d.items() if not k.startswith("_")}
    if isinstance(d, list):
        return [strip_notes(v) for v in d]
    return d


def use(overrides=None, raw=None):
    """configure() from a temp invest.json: the shipped file with `overrides`
    merged per section, or `raw` text as it is. A fresh store each time."""
    if raw is None:
        with open(invest.config_path(), encoding="utf-8") as f:
            data = json.load(f)
        for section, values in (overrides or {}).items():
            if isinstance(values, dict) and isinstance(data.get(section), dict):
                data[section].update(values)
            else:
                data[section] = values
        raw = json.dumps(data)
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(raw)
    cap.lines.clear()
    invest.configure(path)
    invest.DATA_DIR = tempfile.mkdtemp(prefix="denisse-invest-config-")
    return invest.CONFIG


# -- the shipped file -------------------------------------------------------
with open(invest.config_path(), encoding="utf-8") as f:
    shipped = strip_notes(json.load(f))
check("invest.json ships exactly the documented defaults", shipped == invest.DEFAULTS,
      "" if shipped == invest.DEFAULTS else "edit both, or neither")
use()
check("the shipped invest.json loads with no warnings", not cap.lines, "; ".join(cap.lines))
check("shipped config: an end-of-day quote is reused for 6 hours", invest.AlphaVantage.reuse_hours == 6)

# -- changing the data changes the behaviour --------------------------------
use({"chat": {"patterns": {"add": r"^\s*follow\s+{sym}\s*$"}}})
reply = invest.chat("follow ACME.LON", P, NOW)
check("a new add phrasing in invest.json is a command", reply and reply.startswith("Added ACME.LON"), reply)
check("...and the old phrasing no longer is", invest.chat("add ACME.LON to my watchlist", P, NOW) is None)

use({"brief": {"move_sizes": [{"below": 10, "word": "a modest"}], "move_largest": "a huge"}})
check("move size words and bands come from invest.json",
      (invest._move_size(2.0), invest._move_size(-12)) == ("a modest", "a huge"),
      f"{invest._move_size(2.0)!r}, {invest._move_size(-12)!r}")

use({"quotes": {"stale_days": 1}})
inst = {"symbol": "ACME.LON", "currency": "GBX", "utc_offset": 1}
q = invest._observe(inst, P, {"price": 300, "market_date": "2026-10-28"}, NOW, NOW.isoformat(), "delayed")
check("stale_days from invest.json: a 2-day-old quote is stale at 1", q.get("stale") is True, str(q.get("problem")))
use()
q = invest._observe(inst, P, {"price": 300, "market_date": "2026-10-28"}, NOW, NOW.isoformat(), "delayed")
check("...and not stale at the shipped 4", not q.get("stale"))

use({"alerts": {"currency_hint": "pence, not pounds"}, "brief": {"background": "Synthetic background line."}})
invest.add("ACME.LON", P)
reply = invest.set_alert("ACME.LON", "above", "3", "GBP")
check("the currency hint comes from invest.json", "(pence, not pounds)" in reply, reply)
fix = invest.FixtureProvider(quotes={"ACME.LON": {"price": 301, "previous_close": 300, "change_percent": 0.3,
                                                  "market_date": "2026-10-30"}})
out_dir = tempfile.mkdtemp(prefix="denisse-invest-config-briefs-")
said = invest.brief(fix, NOW, out_dir)
saved = [n for n in os.listdir(out_dir) if n.endswith(".md") and not n.endswith(".failed.md")]
text = open(os.path.join(out_dir, saved[0]), encoding="utf-8").read() if saved else ""
check("the brief's background text comes from invest.json", "Synthetic background line." in text, said)

use({"brief": {"headings": {"observations": "### Seen", "alerts": "### Rang", "interpretation": "### Mine",
                            "background": "### Context"}}})
invest.add("ACME.LON", P)
out_dir = tempfile.mkdtemp(prefix="denisse-invest-config-briefs-")
said = invest.brief(fix, NOW, out_dir)
check("new headings: the brief is written and verifies against them", "Full report saved" in said, said)

# -- wording (invest.json "replies" and "fixtures") ------------------------
use({"replies": {"watchlist": {"added": "Watching {symbol} now.", "list_empty": "Nothing watched."}}})
check("a reply's wording comes from invest.json", invest.list_text() == "Nothing watched.", invest.list_text())
reply = invest.add("ACME.LON", P)
check("...with its fields filled in", reply == "Watching ACME.LON now.", reply)
check("...and an unchanged reply keeps its default", invest.add("ACME.LON", P) == "ACME.LON is already on your watchlist.")

AMBIG = invest.FixtureProvider(matches={"ACME": [dict(M, symbol="ACME.LON", name="Acme plc"),
                                                 dict(M, symbol="ACME.US", name="Acme Inc", currency="USD")]})
use({"replies": {"watchlist": {"add_ambiguous": "{query}: several.", "add_ambiguous_ask": " Pick ({numbered})."}}})
q = invest.chat("add ACME to my watchlist", AMBIG, NOW)
check("an ambiguous add is asked about whatever its wording (no phrase match on the reply)",
      q == "ACME: several. Pick (1 = ACME.LON, 2 = ACME.US).", q)
r = invest.chat("2", AMBIG, NOW, last_said=q)
check("...and the answer to that question is taken", (r or "").startswith("Added ACME.US"), r)

use({"replies": {"brief": {"move_note": "- {symbol}: {change:+.1f}% ({size})", "title": "# Brief {stamp}",
                           "stamp_format": "%d/%m/%Y"}}})
invest.add("ACME.LON", P)
out_dir = tempfile.mkdtemp(prefix="denisse-invest-config-briefs-")
said = invest.brief(fix, NOW, out_dir)
saved = [n for n in os.listdir(out_dir) if n.endswith(".md") and not n.endswith(".failed.md")]
text = open(os.path.join(out_dir, saved[0]), encoding="utf-8").read() if saved else ""
check("brief wording and its date format come from invest.json",
      "# Brief 30/10/2026" in text and "- ACME.LON: +0.3% (a small)" in text, said)

use({"fixtures": {"label": "MADE-UP DATA", "demo_instruments": [
    {"search": "TOY", "symbol": "TOY.FIX", "name": "Toy Co", "type": "Equity", "region": "Nowhere",
     "currency": "XXX", "timezone": "UTC+00", "price": 5, "previous_close": 4, "change_percent": 25}]}})
demo = invest.provider(fixtures=True)
check("the --fixtures demo instruments come from invest.json",
      demo.search("TOY")[0]["symbol"] == "TOY.FIX" and demo.quote("TOY.FIX")["price"] == 5
      and not demo.search("DEMO"), str(demo.matches))
check("...and so does the fixture label", invest.FIXTURE_LABEL == "MADE-UP DATA"
      and "MADE-UP DATA" in invest.FixtureProvider.name, invest.FixtureProvider.name)

# -- bad values: a warning and the default, never a crash -------------------
cfg = use({"quotes": {"stale_days": "four"}, "alerts": {"cooldown_hours": -1}, "store": {"lock_attempts": 2.5}})
check("bad numbers fall back to their defaults",
      (invest.STALE_DAYS, invest.COOLDOWN_HOURS, cfg["store"]["lock_attempts"]) == (4, 24, 200))
check("...with one warning naming each key",
      all(any(k in line for line in cap.lines) for k in ("quotes.stale_days", "alerts.cooldown_hours",
                                                          "store.lock_attempts")), "; ".join(cap.lines))

use({"chat": {"patterns": {"alert": r"^alert\s+{sym}$", "remove": r"^(unclosed"}}})
check("a pattern that loses capture groups, or doesn't compile, falls back",
      invest.CONFIG["chat"]["patterns"]["alert"] == invest.DEFAULTS["chat"]["patterns"]["alert"]
      and invest.CONFIG["chat"]["patterns"]["remove"] == invest.DEFAULTS["chat"]["patterns"]["remove"]
      and len(cap.lines) == 2, "; ".join(cap.lines))
check("...and the default command still works",
      (invest.chat("add ACME.LON to my watchlist", P, NOW) or "").startswith("Added"))

use({"brief": {"move_sizes": [{"below": 3, "word": "x"}, {"below": 1, "word": "y"}]}, "nonsense": 1})
check("unordered move sizes fall back; an unknown section is ignored",
      invest._move_size(2.0) == "a noticeable" and len(cap.lines) == 2, "; ".join(cap.lines))

use({"replies": {"watchlist": {"added": "Added {symbol} for {who}.", "removed": "Removed {symbol:zz}."}},
     "fixtures": {"demo_instruments": [{"symbol": "NOPRICE"}]}})
check("a reply using a field it isn't given falls back, with a warning",
      invest.CONFIG["replies"]["watchlist"]["added"] == invest.DEFAULTS["replies"]["watchlist"]["added"]
      and any("replies.watchlist.added" in line and "who" in line for line in cap.lines), "; ".join(cap.lines))
check("an incomplete demo instrument falls back to the default demo",
      invest.CONFIG["fixtures"]["demo_instruments"] == invest.DEFAULTS["fixtures"]["demo_instruments"])
invest.add("ACME.LON", P)
reply = invest.remove("ACME.LON")
check("a bad format spec is caught when used: the default wording, not a crash",
      reply == "Removed ACME.LON from your watchlist."
      and any("replies.watchlist.removed" in line for line in cap.lines), reply)

use(raw="{ not json")
check("an unreadable invest.json: every default, one warning",
      invest.CONFIG == invest.DEFAULTS and len(cap.lines) == 1, "; ".join(cap.lines))
use(raw="[]")
check("a non-object invest.json: every default", invest.CONFIG == invest.DEFAULTS, "; ".join(cap.lines))

invest.configure()
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
