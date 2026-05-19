"""
Paper summarizer.

For each filtered item, asks the LLM to produce a structured JSON record:
  {
    "summary":             3-line abstract in plain English,
    "key_contribution":    one sentence on what's novel,
    "importance_1_to_5":   int rating,
    "importance_rationale": one sentence justifying the rating,
    "your_relevance":      one sentence tying it to the user's research,
  }

The user's research focus is injected into the system prompt so "your_relevance"
is personalized. Change RESEARCH_FOCUS below (or load from config) to retarget.

Provider-agnostic: receives an LLMClient and doesn't care which provider it is.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.llm_client import LLMClient, generate_json_with_retry

logger = logging.getLogger(__name__)


# ---------- Prompts ----------

# Edit RESEARCH_FOCUS to retarget the "your_relevance" comments.
RESEARCH_FOCUS = """\
The reader is a Director of Beyond-5G research working on AI-native air interfaces
and next-generation wireless systems. Their interests span:
  - AI/ML for the physical layer (neural receivers, learned PHY, end-to-end learning)
  - Channel modeling and GAN-based generative channel models
  - CSI compression, channel estimation, beam management, massive MIMO
  - Integrated sensing and communication (ISAC / JCAS)
  - Non-terrestrial networks (NTN, LEO, direct-to-cell)
  - Semantic and task-oriented communication
  - 6G waveforms (OTFS, AFDM, sub-THz)
"""

SYSTEM_PROMPT = f"""\
You are a research analyst summarizing wireless-research literature for a senior
researcher. You produce concise, technically precise summaries -- no marketing
language, no filler.

{RESEARCH_FOCUS}

Output strict JSON (no markdown fences, no commentary outside the JSON object)
matching this schema exactly:

{{
  "summary": "3 short sentences capturing the problem, approach, and result",
  "key_contribution": "one sentence on what is novel or noteworthy",
  "importance_1_to_5": <integer 1-5>,
  "importance_rationale": "one sentence justifying the rating",
  "your_relevance": "one sentence on how this connects to the reader's work,
                    or 'tangential' if it does not"
}}

Importance rubric:
  5 = landmark result, likely to influence 3GPP / commercial 6G design
  4 = strong contribution from a credible group, novel method or dataset
  3 = solid incremental work, well-motivated
  2 = competent but narrow scope
  1 = weak, redundant, or off-topic for 6G research
"""


def _build_user_prompt(item: dict) -> str:
    authors = ", ".join(item.get("authors", [])[:6])
    if len(item.get("authors", [])) > 6:
        authors += ", et al."
    return (
        f"Title: {item.get('title', '(untitled)')}\n"
        f"Authors: {authors or '(none listed)'}\n"
        f"Venue: {item.get('venue', '')}\n"
        f"Matched topics: {', '.join(item.get('matched_topics', []))}\n"
        f"Abstract:\n{item.get('abstract', '(no abstract available)')}\n"
    )


# ---------- Summarize one item ----------

def summarize_one(client: LLMClient, item: dict) -> dict:
    """Adds 'llm' field to item with the structured analysis."""
    user_prompt = _build_user_prompt(item)
    try:
        result = generate_json_with_retry(
            client, SYSTEM_PROMPT, user_prompt, max_tokens=600, retries=3,
        )
        # Defensive: ensure required fields exist
        item["llm"] = {
            "summary": result.get("summary", ""),
            "key_contribution": result.get("key_contribution", ""),
            "importance_1_to_5": int(result.get("importance_1_to_5", 3)),
            "importance_rationale": result.get("importance_rationale", ""),
            "your_relevance": result.get("your_relevance", ""),
        }
    except Exception as e:
        logger.error("Summarization failed for %s: %s", item.get("id"), e)
        # Fall back so the report still renders
        item["llm"] = {
            "summary": item.get("abstract", "")[:400],
            "key_contribution": "(LLM summary unavailable)",
            "importance_1_to_5": 3,
            "importance_rationale": "Fallback rating; LLM call failed.",
            "your_relevance": "(unavailable)",
        }
    return item


# ---------- Summarize a batch (parallel) ----------

def summarize_batch(
    client: LLMClient,
    items: list[dict],
    max_workers: int = 4,
) -> list[dict]:
    """Summarize concurrently. Reorders by importance descending for the report."""
    logger.info("Summarizing %d items with up to %d workers", len(items), max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(summarize_one, client, it): it for it in items}
        done = []
        for fut in as_completed(futures):
            done.append(fut.result())

    # Re-rank: importance from LLM is the primary signal now,
    # with our keyword relevance_score as a tiebreaker.
    done.sort(
        key=lambda x: (x["llm"]["importance_1_to_5"], x.get("relevance_score", 0)),
        reverse=True,
    )
    logger.info("Summarization complete")
    return done


if __name__ == "__main__":
    # Offline smoke test using a stub client (no API key needed).
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from datetime import datetime, timezone

    class StubClient(LLMClient):
        def generate_json(self, system_prompt, user_prompt, max_tokens=1024):
            return {
                "summary": "Stubbed three-sentence summary.",
                "key_contribution": "Stubbed contribution.",
                "importance_1_to_5": 4,
                "importance_rationale": "Stubbed rationale.",
                "your_relevance": "Stubbed relevance.",
            }

    items = [{
        "id": "arxiv:1234",
        "title": "Neural Receiver for 6G PHY",
        "authors": ["A. Researcher"],
        "abstract": "We propose a neural receiver...",
        "venue": "arXiv eess.SP",
        "matched_topics": ["AI-native air interface"],
        "relevance_score": 1.0,
        "published": datetime.now(timezone.utc),
    }]
    out = summarize_batch(StubClient(), items, max_workers=2)
    for o in out:
        print(o["title"], "->", o["llm"])
