"""
arXiv fetcher.

Uses the public arXiv Atom API. No auth required.
Docs: https://info.arxiv.org/help/api/user-manual.html

Returns a list of dicts with the common schema used across all fetchers:
{
    "id":           str,   # canonical id (e.g. "arxiv:2401.12345")
    "title":        str,
    "authors":      list[str],
    "abstract":     str,
    "link":         str,   # abstract page
    "pdf_url":      str,
    "published":    datetime (UTC),
    "source":       "arxiv",
    "venue":        "arXiv <category>",
}
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import feedparser  # arXiv API returns Atom; feedparser handles it cleanly
import requests

logger = logging.getLogger(__name__)

ARXIV_API = "http://export.arxiv.org/api/query"

# arXiv API politeness:
#   - Min spacing between requests: 3 seconds (arXiv asks for this).
#   - On 429 / 5xx, back off and retry.
# We use 5 seconds in practice -- safer on shared CI IPs (GitHub Actions
# runners often share IP pools that arXiv may already rate-limit).
INTER_REQUEST_DELAY = 5
MAX_RETRIES = 5
INITIAL_BACKOFF = 30  # seconds; doubles each retry


def _get_with_retry(url: str, params: dict, headers: dict):
    """GET that retries on 429/5xx with exponential backoff. Returns response or None."""
    backoff = INITIAL_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=60)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                # Honor Retry-After if arXiv sent one, else use our backoff.
                retry_after = resp.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else backoff
                logger.warning(
                    "arXiv returned %d on attempt %d/%d; waiting %ds before retry",
                    resp.status_code, attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                backoff *= 2
                continue
            # Other 4xx: not worth retrying
            logger.error("arXiv returned %d, not retrying: %s",
                         resp.status_code, resp.text[:200])
            return None
        except requests.RequestException as e:
            logger.warning("arXiv request error on attempt %d/%d: %s",
                           attempt, MAX_RETRIES, e)
            time.sleep(backoff)
            backoff *= 2
    return None


def _build_query(categories: list[str], start: int, max_results: int) -> dict:
    cat_query = "+OR+".join(f"cat:{c}" for c in categories)
    return {
        "search_query": cat_query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": start,
        "max_results": max_results,
    }


def _parse_entry(entry, category: str) -> dict | None:
    try:
        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        # arXiv links: prefer the abs page; PDF link has rel="related" type="application/pdf"
        pdf_url = None
        for link in entry.get("links", []):
            if link.get("type") == "application/pdf":
                pdf_url = link.get("href")
                break
        arxiv_id = entry.id.rsplit("/", 1)[-1]
        return {
            "id": f"arxiv:{arxiv_id}",
            "title": entry.title.strip().replace("\n", " "),
            "authors": [a.name for a in entry.get("authors", [])],
            "abstract": entry.summary.strip().replace("\n", " "),
            "link": entry.id,  # abs page
            "pdf_url": pdf_url,
            "published": published,
            "source": "arxiv",
            "venue": f"arXiv {category}",
        }
    except Exception as e:
        logger.warning("Failed to parse arXiv entry: %s", e)
        return None


def fetch(config: dict) -> list[dict]:
    """
    config: the 'arxiv' subtree from sources.yaml
    """
    if not config.get("enabled", True):
        return []

    categories = config["categories"]
    lookback_days = config.get("lookback_days", 7)
    max_per_cat = config.get("max_results_per_category", 200)
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    results: list[dict] = []
    # Query each category separately to respect per-category caps and to tag venue cleanly.
    for cat in categories:
        params = _build_query([cat], start=0, max_results=max_per_cat)
        logger.info("Fetching arXiv category %s (last %d days)", cat, lookback_days)
        headers = {"User-Agent": "6g-weekly-survey/0.1 (research literature agent)"}
        resp = _get_with_retry(ARXIV_API, params, headers)
        if resp is None:
            logger.warning("Skipping category %s after repeated failures", cat)
            continue
        feed = feedparser.parse(resp.content)

        kept = 0
        for entry in feed.entries:
            parsed = _parse_entry(entry, cat)
            if parsed is None:
                continue
            if parsed["published"] < cutoff:
                # Entries are sorted by submittedDate desc, so we can break.
                break
            results.append(parsed)
            kept += 1
        logger.info("  %d papers in window for %s", kept, cat)
        time.sleep(INTER_REQUEST_DELAY)

    logger.info("arXiv total: %d papers", len(results))
    return results


if __name__ == "__main__":
    # Quick smoke test: python -m src.fetchers.arxiv_fetcher
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = {
        "enabled": True,
        "categories": ["eess.SP"],
        "lookback_days": 3,
        "max_results_per_category": 20,
    }
    papers = fetch(cfg)
    for p in papers[:5]:
        print(f"- {p['title']}  ({p['published'].date()})")
    print(f"Total: {len(papers)}")
