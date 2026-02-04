import logging
import ssl
import urllib.request
from datetime import datetime

import feedparser

from .base_scanner import BaseScanner, ScannedItem

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class RSSScanner(BaseScanner):
    """Scanner for RSS/Atom feeds (newsletters, blogs, news sites)."""

    def __init__(self, max_days=180, rate_limit_seconds=1.0):
        super().__init__(rate_limit_seconds=rate_limit_seconds)
        self.max_days = max_days

    def _fetch_feed(self, url: str):
        """Fetch and parse a feed, with SSL fallback for sites with cert issues."""
        feed = feedparser.parse(url, agent=USER_AGENT)
        if feed.bozo and not feed.entries:
            exc = feed.bozo_exception
            # If SSL error, retry with unverified context
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                logger.info("Retrying %s with unverified SSL", url)
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                handler = urllib.request.HTTPSHandler(context=ctx)
                feed = feedparser.parse(url, agent=USER_AGENT, handlers=[handler])
        return feed

    def scan(self, source: dict) -> list[ScannedItem]:
        self._rate_limit()
        url = source["url"]
        logger.info("Scanning RSS feed: %s (%s)", source.get("name", url), url)

        feed = self._fetch_feed(url)
        if feed.bozo and not feed.entries:
            exc = feed.bozo_exception
            logger.warning("Feed parse error for %s: %s", url, exc)
            # Raise so scan_safe can classify it
            raise RuntimeError(f"Feed parse error: {exc}")

        items = []
        for entry in feed.entries:
            published_at = self._parse_date(entry)
            if not self.is_recent(published_at, self.max_days):
                continue

            link = entry.get("link", "")
            if not link:
                continue

            title = entry.get("title", "").strip()
            content = self._extract_content(entry)

            if not content and not title:
                continue

            items.append(ScannedItem(
                url=link,
                title=title,
                content=content[:5000],  # Cap content length
                author=self._extract_author(entry),
                published_at=published_at,
                source_id=source.get("id"),
            ))

        logger.info("Found %d recent items from %s", len(items), source.get("name", url))
        return items

    @staticmethod
    def _parse_date(entry) -> str | None:
        for field in ("published_parsed", "updated_parsed"):
            parsed = entry.get(field)
            if parsed:
                try:
                    dt = datetime(*parsed[:6])
                    return dt.strftime("%Y-%m-%dT%H:%M:%S")
                except Exception:
                    continue
        for field in ("published", "updated"):
            val = entry.get(field)
            if val:
                return val
        return None

    @staticmethod
    def _extract_content(entry) -> str:
        # Try content field first (richer)
        if "content" in entry:
            for c in entry["content"]:
                val = c.get("value", "")
                if val:
                    return _strip_html(val)

        # Fall back to summary
        summary = entry.get("summary", "")
        if summary:
            return _strip_html(summary)

        return entry.get("description", "")

    @staticmethod
    def _extract_author(entry) -> str | None:
        if "author" in entry:
            return entry["author"]
        if "authors" in entry and entry["authors"]:
            return entry["authors"][0].get("name")
        return None


def _strip_html(html: str) -> str:
    """Basic HTML tag removal."""
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
