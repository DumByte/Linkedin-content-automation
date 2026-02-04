"""Daily content scanning and post generation orchestrator."""

import json
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.database import (
    deactivate_sources_not_in,
    get_active_sources,
    get_candidate_pool,
    get_consecutive_zero_count,
    init_db,
    insert_content,
    insert_ranked_candidates,
    insert_rejected_articles,
    insert_source_failure,
    update_source_last_scanned,
    upsert_source,
)
from src.ranker import get_last_rejected, rank_content
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
    active_rss_urls = set()

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

    # RSS sources (all sections with url field)
    for section in ("newsletters", "blogs", "news", "regulatory", "academic", "vc_blogs"):
        for src in config.get(section, []):
            if src.get("enabled") is False:
                continue
            upsert_source(
                name=src["name"],
                url=src["url"],
                source_type="rss",
                category=src.get("category"),
                priority=src.get("priority", 5),
            )
            active_rss_urls.add(src["url"])
            count += 1

    # Deactivate any RSS sources in the DB whose URL is no longer in the config
    # (handles removed sources, changed URLs, and disabled sources)
    if active_rss_urls:
        deactivate_sources_not_in(active_rss_urls)

    logger.info("Loaded %d sources from config", count)


def scan_all_sources():
    """Run all scanners against active sources."""
    rss_scanner = RSSScanner()

    total_items = 0
    failure_count = 0
    zero_count = 0

    # Scan RSS sources
    rss_sources = get_active_sources("rss")
    logger.info("Scanning %d RSS sources...", len(rss_sources))
    for source in rss_sources:
        items, failure_info = rss_scanner.scan_safe(source)

        if failure_info:
            # Hard failure — log it
            failure_count += 1
            insert_source_failure(
                source_id=source["id"],
                source_name=source["name"],
                source_url=source["url"],
                failure_type=failure_info["failure_type"],
                error_message=failure_info["error_message"],
            )
            _append_failure_log(source, failure_info)
        elif len(items) == 0:
            # Soft failure — zero results
            zero_count += 1
            prev_count = get_consecutive_zero_count(source["id"])
            new_count = prev_count + 1
            insert_source_failure(
                source_id=source["id"],
                source_name=source["name"],
                source_url=source["url"],
                failure_type="zero_results",
                error_message=f"Consecutive zero-result runs: {new_count}",
                consecutive_zero_count=new_count,
            )
            if new_count >= 3:
                logger.warning(
                    "Source '%s' has returned 0 articles for %d consecutive runs",
                    source["name"], new_count,
                )
        else:
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
    logger.info("Skipping Twitter sources (free RSS bridges no longer available)")

    logger.info(
        "Scanning complete. %d new items stored. %d hard failures, %d zero-result sources.",
        total_items, failure_count, zero_count,
    )
    return total_items


def _append_failure_log(source: dict, failure_info: dict):
    """Append a failure entry to the dedicated failure log file."""
    import json
    from datetime import datetime, timezone

    log_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "source_failures.jsonl")

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_name": source.get("name", ""),
        "source_url": source.get("url", ""),
        "failure_type": failure_info["failure_type"],
        "error_message": failure_info["error_message"],
    }

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def rank_candidates(top_n=20):
    """Rank the multi-day candidate pool and store top candidates for manual selection."""
    from datetime import date

    pool = get_candidate_pool(days=5)
    if not pool:
        logger.warning("No candidate articles in pool. Skipping ranking.")
        return []

    logger.info("Ranking %d articles from candidate pool (5-day window)...", len(pool))
    top_items = rank_content(pool, top_n=top_n)

    # Store rejected articles for dashboard
    rejected = get_last_rejected()
    if rejected:
        run_date = date.today().isoformat()
        insert_rejected_articles(run_date, rejected)
        logger.info("Stored %d rejected articles for dashboard review.", len(rejected))

    if not top_items:
        logger.warning("No items selected after ranking.")
        return []

    # Store ranked candidates for manual selection via dashboard
    run_date = date.today().isoformat()
    insert_ranked_candidates(run_date, top_items)
    logger.info("Ranked %d candidate articles for manual selection.", len(top_items))

    return top_items


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

    # 3. Rank content from multi-day candidate pool
    rank_candidates(top_n=20)

    logger.info("Daily run complete.")


if __name__ == "__main__":
    run_daily()
