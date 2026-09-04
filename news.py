"""
Denisse -- CNA (Channel NewsAsia) news headlines, on demand and once a day.

Uses CNA's own public RSS feed -- no API key, no scraping. Verified live
before wiring in: https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml

Two things live here:
- fetch_headlines(): the on-demand side, used by tools.get_news().
- is_daily_briefing_due() / mark_daily_briefing_done(): a tiny persisted
  once-per-day gate tray.py uses to decide when to proactively summarize
  the day's news, rather than only ever answering when asked.

Run standalone:
    python news.py
"""

import datetime
import json
import os
import urllib.request
import xml.etree.ElementTree as ET

CNA_RSS_URL = "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BRIEFING_STATE_FILE = os.path.join(BASE_DIR, "memory", "daily_news_briefing.json")

DAILY_BRIEFING_EARLIEST_HOUR = 8  # don't proactively summarize before 8am local time


def fetch_headlines(topic: str = "", count: int = 5) -> list:
    """Fetch the latest CNA headlines, optionally filtered to a topic/
    category (e.g. 'business', 'singapore', 'world') matched against the
    article's title or CNA's own category tag. Returns a list of
    '<title> [<category>]' strings, or raises on a real fetch/parse
    failure -- caller (tools.get_news) turns that into an error string."""
    with urllib.request.urlopen(CNA_RSS_URL, timeout=10) as resp:
        xml_data = resp.read()
    root = ET.fromstring(xml_data)

    headlines = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        category = (item.findtext("category") or "").strip()
        if not title:
            continue
        if topic and topic.lower() not in title.lower() and topic.lower() not in category.lower():
            continue
        headlines.append(f"{title} [{category}]" if category else title)
        if len(headlines) >= count:
            break
    return headlines


def _load_briefing_state() -> dict:
    if not os.path.exists(BRIEFING_STATE_FILE):
        return {}
    try:
        with open(BRIEFING_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def is_daily_briefing_due() -> bool:
    """True once per calendar day, and only from DAILY_BRIEFING_EARLIEST_HOUR
    onward -- so tray.py's background loop can poll this cheaply and only
    actually trigger a briefing once, whenever it first becomes true."""
    now = datetime.datetime.now()
    if now.hour < DAILY_BRIEFING_EARLIEST_HOUR:
        return False
    state = _load_briefing_state()
    return state.get("last_briefing_date") != now.strftime("%Y-%m-%d")


def mark_daily_briefing_done() -> None:
    os.makedirs(os.path.dirname(BRIEFING_STATE_FILE), exist_ok=True)
    with open(BRIEFING_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_briefing_date": datetime.datetime.now().strftime("%Y-%m-%d")}, f, indent=2)


def main() -> None:
    for h in fetch_headlines(count=8):
        print(f"- {h}")


if __name__ == "__main__":
    main()
