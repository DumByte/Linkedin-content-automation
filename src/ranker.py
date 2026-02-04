import logging
import math
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

from dateutil import parser as date_parser

logger = logging.getLogger(__name__)


MAX_AGE_DAYS = 180  # 6 months


def rank_content(items: list[dict], top_n: int = 20) -> list[dict]:
    """Score and rank content items, returning the top N by score.

    Scoring factors:
    - Recency (exponential decay, max 6 months old)
    - Content substance (length, data presence)
    - Source authority (priority from config)
    - Engagement quality (links, mentions, specificity)

    Deduplicates by URL and content similarity before ranking.
    """
    if not items:
        return []

    # Deduplicate
    items = _deduplicate(items)

    # Score each item with full breakdown
    scored = []
    for item in items:
        breakdown = _compute_score_breakdown(item)
        total = sum(breakdown.values())
        if total > 0:
            scored.append({
                **item,
                "engagement_score": total,
                "score_breakdown": breakdown,
            })

    # Sort by score descending
    scored.sort(key=lambda x: x["engagement_score"], reverse=True)

    # Select top N by score (no source diversity filter)
    selected = scored[:top_n]
    rejected = [
        {**item, "rejection_reason": "Outside top %d" % top_n}
        for item in scored[top_n:]
    ]

    logger.info(
        "Ranked %d items, selected top %d (scores: %s)",
        len(scored),
        len(selected),
        [f"{s['engagement_score']:.2f}" for s in selected],
    )

    # Store rejected articles for dashboard visibility (module-level cache)
    global _last_rejected
    _last_rejected = rejected[:20]

    return selected


# Module-level cache for rejected articles from the most recent ranking run
_last_rejected: list[dict] = []


def get_last_rejected() -> list[dict]:
    """Return the top 20 rejected articles from the most recent ranking run."""
    return _last_rejected


def _compute_score(item: dict) -> float:
    return sum(_compute_score_breakdown(item).values())


def _compute_score_breakdown(item: dict) -> dict:
    """Return individual score components as a dict."""
    return {
        "recency": _recency_score(item.get("published_at")),
        "substance": _substance_score(item.get("content", ""), item.get("title", "")),
        "authority": _authority_score(item.get("priority", 5)),
        "engagement": _engagement_score(item.get("content", ""), item.get("title", "")),
    }


def _recency_score(published_at: str | None) -> float:
    """Exponential decay based on age. Max 30 points. Returns 0 for missing/old dates."""
    if not published_at:
        return 0.0  # No date = not usable

    try:
        dt = date_parser.parse(published_at, fuzzy=True)

        # Make naive for comparison
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)

        days_old = (datetime.now(timezone.utc).replace(tzinfo=None) - dt).total_seconds() / 86400

        # Reject anything older than MAX_AGE_DAYS (6 months)
        if days_old > MAX_AGE_DAYS:
            return 0.0

        # Exponential decay over 6 months
        # Full points at 0 days, ~20 at 1 week, ~10 at 1 month, ~2 at 6 months
        return 30.0 * math.exp(-0.02 * max(days_old, 0))
    except Exception:
        return 0.0


def _substance_score(content: str, title: str) -> float:
    """Score based on content depth. Max 25 points."""
    score = 0.0
    text = f"{title} {content}"
    word_count = len(text.split())

    # Length bonus (diminishing returns)
    if word_count > 50:
        score += 5
    if word_count > 150:
        score += 5
    if word_count > 300:
        score += 5

    # Contains numbers/data (suggests concrete information)
    numbers = re.findall(r"\$[\d,.]+[BMK]?|\d+%|\d{4,}", text)
    if numbers:
        score += min(len(numbers) * 2, 5)

    # Contains quotes (suggests insider access)
    if re.search(r'["""\u201c\u201d][^"""\u201c\u201d]+["""\u201c\u201d]', text):
        score += 5

    return min(score, 25.0)


def _authority_score(priority: int) -> float:
    """Score based on source priority config. Max 20 points."""
    # Priority is 1-10, map to 0-20
    return min(priority * 2, 20.0)


def _engagement_score(content: str, title: str) -> float:
    """Score based on engagement-worthy signals. Max 25 points."""
    score = 0.0
    text = f"{title} {content}".lower()

    # High-signal keywords
    high_signal = [
        "breaking", "exclusive", "announced", "launched", "partnership",
        "acquisition", "regulation", "billion", "million", "approval",
        "ban", "investigation", "patent", "settlement",
    ]
    matches = sum(1 for kw in high_signal if kw in text)
    score += min(matches * 3, 10)

    # Topic relevance boosters
    topic_keywords = [
        "stablecoin", "cbdc", "tokenization", "embedded finance",
        "banking as a service", "baas", "real-time payments",
        "cross-border", "defi", "regtech", "open banking",
        "generative ai", "llm", "artificial intelligence",
    ]
    topic_matches = sum(1 for kw in topic_keywords if kw in text)
    score += min(topic_matches * 2, 10)

    # Contains a link (more shareable)
    if re.search(r"https?://", content):
        score += 5

    return min(score, 25.0)


def _deduplicate(items: list[dict]) -> list[dict]:
    """Remove duplicates by URL and content similarity."""
    seen_urls = set()
    unique = []

    for item in items:
        url = item.get("url", "")

        # Exact URL duplicate
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Content similarity check against already-kept items
        content = item.get("content", "")[:500]
        is_dup = False
        for kept in unique:
            kept_content = kept.get("content", "")[:500]
            if content and kept_content:
                similarity = SequenceMatcher(None, content, kept_content).quick_ratio()
                if similarity > 0.8:
                    is_dup = True
                    break

        if not is_dup:
            unique.append(item)

    if len(items) != len(unique):
        logger.info("Deduplication: %d -> %d items", len(items), len(unique))

    return unique
