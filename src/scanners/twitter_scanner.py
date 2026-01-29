import logging
import re

import feedparser

from .base_scanner import BaseScanner, ScannedItem

logger = logging.getLogger(__name__)

# Nitter instances to try (rotate on failure)
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.woodland.cafe",
    "https://nitter.1d4.us",
]

# RSSHub as fallback
RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
]


class TwitterScanner(BaseScanner):
    """Scanner for Twitter/X accounts via Nitter RSS or RSSHub."""

    def __init__(self, max_hours=48, rate_limit_seconds=3.0):
        super().__init__(rate_limit_seconds=rate_limit_seconds)
        self.max_hours = max_hours

    def scan(self, source: dict) -> list[ScannedItem]:
        handle = source.get("handle", "")
        if not handle:
            logger.warning("No handle provided for Twitter source: %s", source.get("name"))
            return []

        logger.info("Scanning Twitter account: @%s", handle)

        # Try Nitter instances first
        items = self._try_nitter(handle, source)
        if items:
            return items

        # Fallback to RSSHub
        items = self._try_rsshub(handle, source)
        if items:
            return items

        logger.warning("All Twitter scan methods failed for @%s", handle)
        return []

    def _try_nitter(self, handle: str, source: dict) -> list[ScannedItem] | None:
        for instance in NITTER_INSTANCES:
            self._rate_limit()
            url = f"{instance}/{handle}/rss"
            try:
                feed = feedparser.parse(url)
                if feed.entries:
                    return self._parse_feed(feed, handle, source)
            except Exception as e:
                logger.debug("Nitter instance %s failed for @%s: %s", instance, handle, e)
                continue
        return None

    def _try_rsshub(self, handle: str, source: dict) -> list[ScannedItem] | None:
        for instance in RSSHUB_INSTANCES:
            self._rate_limit()
            url = f"{instance}/twitter/user/{handle}"
            try:
                feed = feedparser.parse(url)
                if feed.entries:
                    return self._parse_feed(feed, handle, source)
            except Exception as e:
                logger.debug("RSSHub instance %s failed for @%s: %s", instance, handle, e)
                continue
        return None

    def _parse_feed(self, feed, handle: str, source: dict) -> list[ScannedItem]:
        items = []
        for entry in feed.entries:
            published_at = self._parse_date(entry)
            if not self.is_recent(published_at, self.max_hours):
                continue

            link = entry.get("link", "")
            content = entry.get("title", "") or entry.get("summary", "")
            if not content:
                continue

            # Clean content
            content = re.sub(r"<[^>]+>", " ", content)
            content = re.sub(r"\s+", " ", content).strip()

            # Skip plain retweets (no added commentary before "RT")
            if content.startswith("RT @"):
                continue

            items.append(ScannedItem(
                url=link or f"https://twitter.com/{handle}",
                title=f"@{handle}: {content[:100]}",
                content=content[:2000],
                author=source.get("name", handle),
                published_at=published_at,
                source_id=source.get("id"),
            ))

        logger.info("Found %d recent tweets from @%s", len(items), handle)
        return items

    @staticmethod
    def _parse_date(entry) -> str | None:
        from datetime import datetime
        for field in ("published_parsed", "updated_parsed"):
            parsed = entry.get(field)
            if parsed:
                try:
                    dt = datetime(*parsed[:6])
                    return dt.strftime("%Y-%m-%dT%H:%M:%S")
                except Exception:
                    continue
        return entry.get("published") or entry.get("updated")
