"""Daily content scanning and post generation orchestrator."""

import json
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.content_generator import ContentGenerator
from src.database import (
    get_active_sources,
    init_db,
    insert_content,
    insert_post,
    mark_content_selected,
    get_recent_content,
    update_source_last_scanned,
    upsert_source,
)
from src.ranker import rank_content
from src.scanners.rss_scanner import RSSScanner
# Twitter scanning disabled - Nitter/RSSHub no longer work after Twitter API changes
# from src.scanners.twitter_scanner import TwitterScanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "sources.json")

_config_cache = None


def _load_config():
    """Load and cache config from sources.json."""
    global _config_cache
    if _config_cache is None:
        with open(CONFIG_PATH, "r") as f:
            _config_cache = json.load(f)
    return _config_cache


def load_sources_from_config():
    """Load sources from config/sources.json into the database."""
    config = _load_config()

    count = 0

    # Twitter accounts
    for acct in config.get("twitter_accounts", []):
        url = f"https://twitter.com/{acct['handle']}"
        upsert_source(
            name=acct["name"],
            url=url,
            source_type="twitter",
            category=acct.get("category"),
            priority=acct.get("priority", 5),
        )
        count += 1

    # RSS sources (newsletters, blogs, news)
    for section in ("newsletters", "blogs", "news"):
        for src in config.get(section, []):
            upsert_source(
                name=src["name"],
                url=src["url"],
                source_type="rss",
                category=src.get("category"),
                priority=src.get("priority", 5),
            )
            count += 1

    logger.info("Loaded %d sources from config", count)


def scan_all_sources():
    """Run all scanners against active sources."""
    rss_scanner = RSSScanner()

    total_items = 0

    # Scan RSS sources
    rss_sources = get_active_sources("rss")
    logger.info("Scanning %d RSS sources...", len(rss_sources))
    for source in rss_sources:
        items = rss_scanner.scan_safe(source)
        for item in items:
            content_id = insert_content(
                source_id=source["id"],
                url=item.url,
                title=item.title,
                content=item.content,
                author=item.author,
                published_at=item.published_at,
            )
            if content_id:
                total_items += 1
        update_source_last_scanned(source["id"])

    # Twitter scanning disabled - Nitter/RSSHub no longer work after Twitter API changes
    # To re-enable, you'll need a paid Twitter API or alternative scraping solution
    logger.info("Skipping Twitter sources (free RSS bridges no longer available)")

    logger.info("Scanning complete. %d new items stored.", total_items)
    return total_items


def select_and_generate(top_n=3):
    """Select top content and generate LinkedIn posts."""
    recent = get_recent_content(hours=48)
    if not recent:
        logger.warning("No recent content found. Skipping generation.")
        return []

    logger.info("Ranking %d recent items...", len(recent))
    top_items = rank_content(recent, top_n=top_n)

    if not top_items:
        logger.warning("No items selected after ranking.")
        return []

    # Mark selected
    selected_ids = [item["id"] for item in top_items]
    mark_content_selected(selected_ids)

    # Generate posts
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set. Cannot generate posts.")
        return []

    generator = ContentGenerator(api_key=api_key)
    results = generator.generate_batch(top_items)

    # Store generated posts
    post_ids = []
    for result in results:
        content_item = result["content_item"]
        post_id = insert_post(
            content_id=content_item["id"],
            source_summary=result["source_summary"],
            commentary=result["commentary"],
            full_post=result["full_post"],
        )
        post_ids.append(post_id)
        logger.info("Generated post #%d for: %s", post_id, content_item.get("title", "")[:60])

    return post_ids


def run_daily():
    """Full daily workflow."""
    logger.info("=" * 60)
    logger.info("Starting daily content scan and generation")
    logger.info("=" * 60)

    # 1. Initialize DB and load sources
    init_db()
    load_sources_from_config()

    # 2. Scan all sources
    new_items = scan_all_sources()

    # 3. Select top content and generate posts
    if new_items > 0:
        post_ids = select_and_generate(top_n=3)
        logger.info("Generated %d LinkedIn post drafts.", len(post_ids))
    else:
        logger.info("No new content found. No posts generated.")

    logger.info("Daily run complete.")


if __name__ == "__main__":
    run_daily()
