import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from dateutil import parser as date_parser

logger = logging.getLogger(__name__)


@dataclass
class ScannedItem:
    url: str
    title: str
    content: str
    author: Optional[str] = None
    published_at: Optional[str] = None
    source_id: Optional[int] = None
    metadata: dict = field(default_factory=dict)


class BaseScanner(ABC):
    """Abstract base class for all content scanners."""

    def __init__(self, rate_limit_seconds=2.0):
        self.rate_limit_seconds = rate_limit_seconds
        self._last_request_time = 0.0

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit_seconds:
            time.sleep(self.rate_limit_seconds - elapsed)
        self._last_request_time = time.time()

    @abstractmethod
    def scan(self, source: dict) -> list[ScannedItem]:
        """Scan a source and return discovered items.

        Args:
            source: dict with keys like url, name, source_type, category, id

        Returns:
            List of ScannedItem objects
        """
        ...

    def scan_safe(self, source: dict) -> list[ScannedItem]:
        """Scan with error handling — never raises."""
        try:
            return self.scan(source)
        except Exception as e:
            logger.error("Scanner %s failed for %s: %s", self.__class__.__name__, source.get("url"), e)
            return []

    @staticmethod
    def is_recent(published_at: Optional[str], max_days=180) -> bool:
        """Check if a publication date is within the recency window (default 6 months)."""
        if not published_at:
            return False  # No date = skip it
        try:
            if isinstance(published_at, str):
                dt = date_parser.parse(published_at, fuzzy=True)
            else:
                dt = published_at

            # Make naive for comparison
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)

            age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - dt).total_seconds() / 86400
            return age_days <= max_days
        except Exception:
            return False
