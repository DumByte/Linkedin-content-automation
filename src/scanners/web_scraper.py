import logging
import re

import requests

from .base_scanner import BaseScanner, ScannedItem

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LinkedInContentBot/1.0; +research)"
}


class WebScraper(BaseScanner):
    """Fallback scraper for sources without RSS feeds.

    Uses trafilatura for content extraction with requests as transport.
    """

    def __init__(self, max_hours=48, rate_limit_seconds=3.0):
        super().__init__(rate_limit_seconds=rate_limit_seconds)
        self.max_hours = max_hours

    def scan(self, source: dict) -> list[ScannedItem]:
        url = source["url"]
        logger.info("Web scraping: %s (%s)", source.get("name", url), url)
        self._rate_limit()

        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("Failed to fetch %s: %s", url, e)
            return []

        html = resp.text

        # Try trafilatura first for clean extraction
        content, title = self._extract_with_trafilatura(html, url)

        # Fallback to basic BeautifulSoup extraction
        if not content:
            content, title = self._extract_with_bs4(html)

        if not content:
            logger.warning("No content extracted from %s", url)
            return []

        return [ScannedItem(
            url=url,
            title=title or source.get("name", ""),
            content=content[:5000],
            author=source.get("name"),
            published_at=None,
            source_id=source.get("id"),
        )]

    @staticmethod
    def _extract_with_trafilatura(html: str, url: str) -> tuple[str, str | None]:
        try:
            import trafilatura
            # Extract text and metadata in a single call
            result = trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
                with_metadata=True,
                output_format="json",
            )
            if result:
                import json
                try:
                    data = json.loads(result)
                    text = data.get("text") or data.get("raw_text", "")
                    title = data.get("title")
                    return text, title
                except (json.JSONDecodeError, TypeError):
                    # Fallback: treat result as plain text
                    return result, None
        except ImportError:
            logger.debug("trafilatura not installed, using bs4 fallback")
        except Exception as e:
            logger.debug("trafilatura extraction failed: %s", e)
        return "", None

    @staticmethod
    def _extract_with_bs4(html: str) -> tuple[str, str | None]:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            # Remove scripts, styles, nav
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()

            title = None
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True)

            # Try article tag first
            article = soup.find("article")
            if article:
                text = article.get_text(separator=" ", strip=True)
            else:
                # Fall back to main or body
                main = soup.find("main") or soup.find("body")
                text = main.get_text(separator=" ", strip=True) if main else ""

            # Clean whitespace
            text = re.sub(r"\s+", " ", text).strip()
            return text, title
        except ImportError:
            logger.debug("BeautifulSoup not installed")
        except Exception as e:
            logger.debug("BS4 extraction failed: %s", e)
        return "", None
