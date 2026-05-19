"""
Filter, score, and deduplicate fetched papers/posts.

Pipeline:
  1. Drop items already seen in previous weeks (SQLite seen-store).
  2. Match each item against topics from topics.yaml using keyword scan
     on title + abstract.
  3. Compute a relevance score:
        score = sum(weight_i for each topic matched) * source_multiplier
     where source_multiplier slightly favors arXiv/IEEE over vendor blogs
     when both match the same number of topics (vendor posts are usually
     marketing-heavy; the LLM stage will still rate them, but we want
     research papers to surface first when relevance ties).
  4. Sort by score descending and return top N (max_papers from config).
  5. Dedup near-duplicates by normalized title (papers that appear on
     both arXiv and a publisher feed).

Returned items have two extra fields:
  - "matched_topics": list[str]
  - "relevance_score": float
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SOURCE_MULTIPLIER = {
    "arxiv": 1.0,
    "ieee": 1.0,
    "vendor": 0.85,
    "rss": 0.9,
    "scholar": 0.95,
}


# ---------- Seen-store (SQLite) ----------

class SeenStore:
    """Tracks IDs of items already reported, to avoid resending across weeks."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                id TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                title TEXT
            )
        """)
        self._conn.commit()

    def filter_unseen(self, items: list[dict]) -> list[dict]:
        cur = self._conn.cursor()
        out = []
        for item in items:
            cur.execute("SELECT 1 FROM seen WHERE id = ?", (item["id"],))
            if cur.fetchone() is None:
                out.append(item)
        return out

    def mark_seen(self, items: list[dict]) -> None:
        cur = self._conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cur.executemany(
            "INSERT OR IGNORE INTO seen (id, first_seen, title) VALUES (?, ?, ?)",
            [(it["id"], now, it.get("title", "")[:300]) for it in items],
        )
        self._conn.commit()

    def close(self):
        self._conn.close()


# ---------- Topic matching ----------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def _match_topics(item: dict, topics: list[dict]) -> tuple[list[str], float]:
    """Return (matched_topic_names, raw_score)."""
    haystack = _normalize(f"{item.get('title', '')} {item.get('abstract', '')}")
    matched: list[str] = []
    score = 0.0
    for topic in topics:
        name = topic["name"]
        weight = float(topic.get("weight", 1.0))
        for kw in topic.get("keywords", []):
            if kw.lower() in haystack:
                matched.append(name)
                score += weight
                break   # one keyword hit per topic is enough
    return matched, score


# ---------- Near-duplicate detection (by normalized title) ----------

_PUNCT_RE = re.compile(r"[^\w\s]")

def _title_key(title: str) -> str:
    return _PUNCT_RE.sub("", title.lower()).strip()


def _dedup_by_title(items: list[dict]) -> list[dict]:
    seen_keys: dict[str, dict] = {}
    for it in items:
        key = _title_key(it.get("title", ""))
        if not key:
            continue
        existing = seen_keys.get(key)
        if existing is None:
            seen_keys[key] = it
        else:
            # Keep the higher-scoring source (arXiv preprint over vendor mention, etc.)
            if it.get("relevance_score", 0) > existing.get("relevance_score", 0):
                seen_keys[key] = it
    return list(seen_keys.values())


# ---------- Main entry point ----------

def filter_and_rank(
    items: list[dict],
    topics: list[dict],
    seen_store: SeenStore,
    max_items: int = 20,
    min_score: float = 0.5,
) -> list[dict]:
    """
    Drop seen items, score by topic match, dedup, sort, and cap.
    Does NOT mark items as seen -- caller should do that AFTER the report
    is successfully sent, so a failed send doesn't suppress them next week.
    """
    initial = len(items)
    items = seen_store.filter_unseen(items)
    logger.info("Filter: %d -> %d after seen-store", initial, len(items))

    scored: list[dict] = []
    for it in items:
        matched, raw = _match_topics(it, topics)
        if not matched:
            continue
        mult = SOURCE_MULTIPLIER.get(it.get("source", ""), 0.8)
        it["matched_topics"] = sorted(set(matched))
        it["relevance_score"] = round(raw * mult, 3)
        if it["relevance_score"] >= min_score:
            scored.append(it)
    logger.info("Filter: %d items matched at least one topic above min_score=%.2f",
                len(scored), min_score)

    deduped = _dedup_by_title(scored)
    if len(deduped) < len(scored):
        logger.info("Filter: %d -> %d after title dedup", len(scored), len(deduped))

    deduped.sort(key=lambda x: (x["relevance_score"], x.get("published", datetime.min.replace(tzinfo=timezone.utc))),
                 reverse=True)

    capped = deduped[:max_items]
    logger.info("Filter: returning top %d (of %d eligible)", len(capped), len(deduped))
    return capped


if __name__ == "__main__":
    # Self-test
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from datetime import timezone as tz
    fake_topics = [
        {"name": "NTN", "weight": 1.0, "keywords": ["NTN", "non-terrestrial"]},
        {"name": "Semantic", "weight": 1.0, "keywords": ["semantic communication"]},
    ]
    fake_items = [
        {"id": "a1", "title": "NTN coverage for 6G",
         "abstract": "Non-terrestrial network analysis.",
         "source": "arxiv", "published": datetime.now(tz.utc)},
        {"id": "a2", "title": "Unrelated paper on graph theory",
         "abstract": "Graphs and stuff.", "source": "arxiv",
         "published": datetime.now(tz.utc)},
        {"id": "v1", "title": "NTN coverage for 6G",   # duplicate of a1
         "abstract": "Vendor blog about NTN.",
         "source": "vendor", "published": datetime.now(tz.utc)},
        {"id": "a3", "title": "Semantic communication advances",
         "abstract": "Task-oriented semantic communication for 6G.",
         "source": "arxiv", "published": datetime.now(tz.utc)},
    ]
    store = SeenStore("/tmp/test_seen.sqlite")
    result = filter_and_rank(fake_items, fake_topics, store, max_items=10)
    for r in result:
        print(f"  [{r['relevance_score']}] {r['title']} ({r['source']}) topics={r['matched_topics']}")
    store.close()
    Path("/tmp/test_seen.sqlite").unlink(missing_ok=True)
