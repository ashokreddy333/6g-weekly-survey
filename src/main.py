"""
6G Weekly Survey -- main orchestrator.

Runs the full pipeline:
  1. Load configs.
  2. Fetch from all enabled sources (arXiv, vendor blogs, RSS).
  3. Filter against seen-store and topic keywords; rank.
  4. Summarize survivors with the LLM (provider-agnostic).
  5. Build HTML + PDF report.
  6. Send via Gmail SMTP.
  7. Only after successful send: mark items as seen.

Run with:
  python -m src.main

Required env vars:
  ANTHROPIC_API_KEY  (or OPENAI_API_KEY / GOOGLE_API_KEY depending on provider)
  GMAIL_APP_PASSWORD
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from src.fetchers import arxiv_fetcher, vendor_fetcher
from src.filter import SeenStore, filter_and_rank
from src.llm_client import make_client
from src.mailer import send_report
from src.report import build_html, build_pdf
from src.summarizer import summarize_batch

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name) as f:
        return yaml.safe_load(f)


def _week_label() -> tuple[str, str]:
    """Returns (human_label, slug) e.g. ('May 12 - May 18, 2026', '2026-05-18')."""
    import os
    override = os.environ.get("SURVEY_DATE")
    today = date.fromisoformat(override) if override else date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    label = f"{monday.strftime('%b %d')} - {sunday.strftime('%b %d, %Y')}"
    slug = sunday.isoformat()
    return label, slug


def run() -> int:
    cfg = _load_yaml("config.yaml")
    sources_cfg = _load_yaml("sources.yaml")
    topics_cfg = _load_yaml("topics.yaml")
    recipients_cfg = _load_yaml("recipients.yaml")

    week_label, week_slug = _week_label()
    logger.info("=== 6G Weekly Survey: %s ===", week_label)

    # --- Fetch ---
    all_items: list[dict] = []
    if sources_cfg.get("arxiv", {}).get("enabled"):
        all_items.extend(arxiv_fetcher.fetch(sources_cfg["arxiv"]))
    if sources_cfg.get("vendor_sources", {}).get("enabled"):
        all_items.extend(vendor_fetcher.fetch(sources_cfg["vendor_sources"]))
    # rss_feeds, ieee_xplore, scholar_alerts can be added once their fetchers exist.
    # The orchestrator stays unchanged when you wire those in.
    logger.info("Fetched %d total items", len(all_items))

    if not all_items:
        logger.warning("No items fetched -- check source configs and network.")

    # --- Filter & rank ---
    seen_db_path = REPO_ROOT / cfg["paths"]["seen_db"]
    store = SeenStore(seen_db_path)
    try:
        ranked = filter_and_rank(
            all_items,
            topics_cfg["topics"],
            store,
            max_items=cfg["filter"]["max_items"],
            min_score=cfg["filter"]["min_score"],
        )
    finally:
        # Keep store handle open until after successful send.
        pass

    # --- Summarize ---
    if ranked:
        provider = cfg["llm"]["provider"]
        model = cfg["llm"].get("model") or None
        client = make_client(provider, model=model)
        ranked = summarize_batch(client, ranked, max_workers=cfg["llm"].get("max_workers", 4))
    else:
        logger.info("Nothing to summarize this week.")

    # --- Build report ---
    html_body = build_html(ranked, week_label)
    pdf_path = REPO_ROOT / cfg["paths"]["pdf_output"].format(week=week_slug)
    build_pdf(ranked, week_label, pdf_path)

    # --- Send ---
    subject = cfg["email"]["subject_template"].format(week=week_label)
    try:
        send_report(recipients_cfg, subject, html_body, pdf_path)
    except Exception as e:
        logger.exception("Email send failed: %s", e)
        store.close()
        return 1

    # --- Mark seen (only after successful send) ---
    store.mark_seen(ranked)
    store.close()

    logger.info("Done. Reported %d items.", len(ranked))
    return 0


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(run())


if __name__ == "__main__":
    main()
