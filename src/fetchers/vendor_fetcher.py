"""
Vendor sources fetcher.

Handles vendor blogs and white-paper listing pages (Qualcomm, Ericsson, Nokia,
Huawei, MediaTek, etc). Two modes:

  * "rss"  -> standard RSS/Atom feed (Qualcomm OnQ, Ericsson Blog).
  * "html" -> scrape a listing page using CSS selectors from sources.yaml.

Output uses the common schema (see arxiv_fetcher).

NOTE on HTML mode: vendor page markup changes occasionally. The selectors in
sources.yaml are educated guesses; run the fetcher once and inspect output.
If a vendor returns zero items, open the listing page in a browser, inspect
the cards, and update the selectors.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

logger = logging.getLogger(__name__)

USER_AGENT = "6g-weekly-survey/0.1 (research literature agent; contact: ashok)"
REQUEST_HEADERS = {"User-Agent": USER_AGENT}


def _stable_id(name: str, url: str) -> str:
    h = hashlib.sha1(f"{name}|{url}".encode()).hexdigest()[:16]
    return f"vendor:{h}"


def _source_cutoff(source: dict, default_cutoff: datetime) -> datetime:
    """Allow per-source lookback_days to override the top-level default."""
    days = source.get("lookback_days")
    if days is not None:
        return datetime.now(timezone.utc) - timedelta(days=int(days))
    return default_cutoff


def _parse_rss(source: dict, cutoff: datetime) -> list[dict]:
    name = source["name"]
    url = source["url"]
    logger.info("Fetching vendor RSS: %s", name)
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("  RSS fetch failed for %s: %s", name, e)
        return []

    feed = feedparser.parse(resp.content)
    out: list[dict] = []
    for entry in feed.entries:
        # feedparser exposes published_parsed or updated_parsed
        when_struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if not when_struct:
            continue
        published = datetime(*when_struct[:6], tzinfo=timezone.utc)
        if published < cutoff:
            continue

        link = entry.get("link", "")
        title = entry.get("title", "").strip()
        summary = entry.get("summary", "")
        # strip HTML from summary if present
        if summary:
            summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)

        out.append({
            "id": _stable_id(name, link or title),
            "title": title,
            "authors": [],   # vendor posts usually don't expose authors in feed
            "abstract": summary[:2000],
            "link": link,
            "pdf_url": None,
            "published": published,
            "source": "vendor",
            "venue": name,
        })
    logger.info("  %d posts in window", len(out))
    return out


def _parse_html(source: dict, cutoff: datetime) -> list[dict]:
    name = source["name"]
    url = source["url"]
    logger.info("Scraping vendor HTML: %s", name)
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("  HTML fetch failed for %s: %s", name, e)
        return []

    soup = BeautifulSoup(resp.content, "html.parser")
    items = soup.select(source.get("item_selector", "article"))
    if not items:
        logger.warning("  no items matched selector for %s; review markup", name)
        return []

    out: list[dict] = []
    for item in items:
        # Title
        title_el = item.select_one(source.get("title_selector", "h2, h3"))
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        # Link (absolute URL) -- the item itself may be the <a> tag
        if item.name == "a" and item.get("href"):
            href = item.get("href")
        else:
            link_el = item.select_one(source.get("link_selector", "a"))
            href = link_el.get("href") if link_el else None
        if not href:
            continue
        link = urljoin(url, href)

        # Date - if absent, we'll keep the item but stamp it as "now"
        # to avoid losing fresh posts on pages that don't expose dates.
        date_sel = source.get("date_selector")
        published = None
        if date_sel:
            date_el = item.select_one(date_sel)
            if date_el:
                raw = date_el.get("datetime") or date_el.get_text(strip=True)
                try:
                    published = dateparser.parse(raw)
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    pass

        if published is None:
            # No reliable date -> assume recent so we don't drop it.
            published = datetime.now(timezone.utc)
        if published < cutoff:
            continue

        # Short abstract: try a paragraph inside the card
        para = item.find("p")
        abstract = para.get_text(" ", strip=True) if para else ""

        out.append({
            "id": _stable_id(name, link),
            "title": title,
            "authors": [],
            "abstract": abstract[:2000],
            "link": link,
            "pdf_url": None,
            "published": published,
            "source": "vendor",
            "venue": name,
        })

    logger.info("  %d posts in window", len(out))
    return out


def fetch(config: dict) -> list[dict]:
    """
    config: the 'vendor_sources' subtree from sources.yaml
    """
    if not config.get("enabled", True):
        return []

    lookback_days = config.get("lookback_days", 14)
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    results: list[dict] = []
    for source in config.get("sources", []):
        mode = source.get("mode", "rss")
        src_cutoff = _source_cutoff(source, cutoff)
        try:
            if mode == "rss":
                results.extend(_parse_rss(source, src_cutoff))
            elif mode == "html":
                results.extend(_parse_html(source, src_cutoff))
            else:
                logger.warning("Unknown vendor mode '%s' for %s", mode, source["name"])
        except Exception as e:
            logger.exception("Vendor source %s failed: %s", source.get("name"), e)
        time.sleep(2)   # polite spacing

    logger.info("Vendor total: %d posts", len(results))
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = {
        "enabled": True,
        "lookback_days": 30,
        "sources": [
            {"name": "Ericsson Blog", "mode": "rss",
             "url": "https://www.ericsson.com/en/blog/feed"},
        ],
    }
    posts = fetch(cfg)
    for p in posts[:5]:
        print(f"- [{p['venue']}] {p['title']}  ({p['published'].date()})")
    print(f"Total: {len(posts)}")
