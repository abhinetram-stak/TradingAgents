"""Economic Times RSS news feeds for Indian market news.

Three live feeds (50 articles each, updated throughout the trading day):
  - ET Markets   : broad equity, bonds, currencies, indices
  - ET Stocks    : stock-specific stories (earnings, dividends, targets, upgrades)
  - ET Economy   : macro (RBI policy, GDP, FDI, inflation, budget)
"""

import html
import xml.etree.ElementTree as _XML
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import requests
import yfinance as yf

_ET_MARKETS_URL = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"
_ET_STOCKS_URL  = "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"
_ET_ECONOMY_URL = "https://economictimes.indiatimes.com/news/economy/indicators/rssfeeds/1373380680.cms"

_TIMEOUT = 10
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TradingAgentsIndia/1.0)"}

# Module-level cache so repeated calls within one analysis run don't re-fetch yfinance info
_name_cache: dict[str, list[str]] = {}


# ---------------------------------------------------------------------------
# Feed fetching & parsing
# ---------------------------------------------------------------------------

def _fetch_feed(url: str) -> list[dict]:
    """Fetch an RSS 2.0 feed and return a list of article dicts."""
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        root = _XML.fromstring(resp.content)
    except Exception:
        return []

    items = []
    for item in root.iter("item"):
        title   = html.unescape((item.findtext("title")       or "").strip())
        desc    = html.unescape((item.findtext("description") or "").strip())
        link    =               (item.findtext("link")        or "").strip()
        pub_str =               (item.findtext("pubDate")     or "").strip()

        pub_date = None
        if pub_str:
            try:
                pub_date = parsedate_to_datetime(pub_str).replace(tzinfo=None)
            except Exception:
                pass

        items.append({"title": title, "description": desc, "link": link, "pub_date": pub_date})

    return items


# ---------------------------------------------------------------------------
# Company keyword extraction
# ---------------------------------------------------------------------------

def _keywords_for(ticker: str) -> list[str]:
    """
    Build a list of search keywords for a ticker.
    e.g. "HDFCBANK.NS" → ["HDFCBANK", "HDFC Bank Limited", "HDFC Bank", "HDFC"]
    """
    if ticker in _name_cache:
        return _name_cache[ticker]

    base = ticker.split(".")[0].upper()   # strip .NS / .BO suffix
    kws: list[str] = [base]

    try:
        info = yf.Ticker(ticker).info
        for key in ("shortName", "longName"):
            name = (info.get(key) or "").strip()
            if name and name not in kws:
                kws.append(name)
                # First significant word: "Reliance" from "Reliance Industries Limited"
                first = name.split()[0]
                if len(first) > 3 and first not in kws:
                    kws.append(first)
    except Exception:
        pass

    _name_cache[ticker] = kws
    return kws


def _matches(article: dict, keywords: list[str]) -> bool:
    """True if any keyword appears in the article title or description."""
    text = f"{article['title']} {article['description']}".lower()
    return any(kw.lower() in text for kw in keywords)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_articles(articles: list[dict], header: str) -> str:
    lines = [f"## {header}\n"]
    for a in articles:
        lines.append(f"### {a['title']}")
        if a["description"]:
            lines.append(a["description"])
        if a["link"]:
            lines.append(f"Link: {a['link']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API (mirrors yfinance_news.py signatures)
# ---------------------------------------------------------------------------

def get_news_rss(ticker: str, start_date: str, end_date: str) -> str:
    """
    Fetch ET Markets + ET Stocks RSS feeds and filter articles that mention
    the given company. Returns a formatted markdown string for the LLM.

    Args:
        ticker:     NSE/BSE ticker (e.g. "RELIANCE.NS", "HDFCBANK.NS")
        start_date: yyyy-mm-dd
        end_date:   yyyy-mm-dd
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt   = datetime.strptime(end_date,   "%Y-%m-%d") + timedelta(days=1)
    keywords = _keywords_for(ticker)

    # Fetch both stock-focused feeds (100 articles combined)
    raw: list[dict] = []
    for url in [_ET_MARKETS_URL, _ET_STOCKS_URL]:
        raw.extend(_fetch_feed(url))

    seen: set[str] = set()
    matched: list[dict] = []
    for a in raw:
        if a["title"] in seen:
            continue
        if a["pub_date"] and not (start_dt <= a["pub_date"] <= end_dt):
            continue
        if _matches(a, keywords):
            seen.add(a["title"])
            matched.append(a)

    if not matched:
        return (
            f"No Economic Times news found for {ticker} "
            f"(searched: {', '.join(keywords)}) between {start_date} and {end_date}. "
            f"The stock may not have featured in ET's top stories for this period."
        )

    return _format_articles(
        matched,
        f"{ticker} News — Economic Times, {start_date} to {end_date}",
    )


def get_global_news_rss(curr_date: str, look_back_days: int = 7, limit: int = 10) -> str:
    """
    Fetch all three ET RSS feeds for India market + macro news.

    Args:
        curr_date:      yyyy-mm-dd
        look_back_days: how far back to include articles
        limit:          max articles to return
    """
    curr_dt  = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - timedelta(days=look_back_days)

    # All three feeds: market breadth + stock stories + macro/economy
    raw: list[dict] = []
    for url in [_ET_MARKETS_URL, _ET_STOCKS_URL, _ET_ECONOMY_URL]:
        raw.extend(_fetch_feed(url))

    seen: set[str] = set()
    filtered: list[dict] = []
    for a in raw:
        if a["title"] in seen:
            continue
        if a["pub_date"] and not (start_dt <= a["pub_date"] <= curr_dt + timedelta(days=1)):
            continue
        seen.add(a["title"])
        filtered.append(a)
        if len(filtered) >= limit:
            break

    if not filtered:
        return (
            f"No Economic Times news found between "
            f"{start_dt.strftime('%Y-%m-%d')} and {curr_date}."
        )

    return _format_articles(
        filtered,
        f"India Market News — Economic Times, "
        f"{start_dt.strftime('%Y-%m-%d')} to {curr_date}",
    )
