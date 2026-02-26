from __future__ import annotations

import logging
import re
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional, Union

import requests
from requests.adapters import HTTPAdapter, Retry

log = logging.getLogger("METADATA")

HEADERS = {"User-Agent": "unshackle-tags/1.0"}

STRIP_RE = re.compile(r"[^a-z0-9]+", re.I)
YEAR_RE = re.compile(r"\s*\(?[12][0-9]{3}\)?$")


@dataclass
class ExternalIds:
    """Normalized external IDs across providers."""

    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    tmdb_kind: Optional[str] = None  # "movie" or "tv"
    tvdb_id: Optional[int] = None


@dataclass
class MetadataResult:
    """Unified metadata result from any provider."""

    title: Optional[str] = None
    year: Optional[int] = None
    kind: Optional[str] = None  # "movie" or "tv"
    external_ids: ExternalIds = field(default_factory=ExternalIds)
    source: str = ""  # provider name, e.g. "tmdb", "simkl", "imdbapi"
    raw: Optional[dict] = None  # original API response for caching


class MetadataProvider(metaclass=ABCMeta):
    """Abstract base for metadata providers."""

    NAME: str = ""
    REQUIRES_KEY: bool = True

    def __init__(self) -> None:
        self.log = logging.getLogger(f"METADATA.{self.NAME.upper()}")
        self._session: Optional[requests.Session] = None

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(HEADERS)
            retry = Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET", "POST"],
            )
            adapter = HTTPAdapter(max_retries=retry)
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)
        return self._session

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider has the credentials/keys it needs."""

    @abstractmethod
    def search(self, title: str, year: Optional[int], kind: str) -> Optional[MetadataResult]:
        """Search for a title and return metadata, or None on failure/no match."""

    @abstractmethod
    def get_by_id(self, provider_id: Union[int, str], kind: str) -> Optional[MetadataResult]:
        """Fetch metadata by this provider's native ID."""

    @abstractmethod
    def get_external_ids(self, provider_id: Union[int, str], kind: str) -> ExternalIds:
        """Fetch external IDs for a title by this provider's native ID."""


def _clean(s: str) -> str:
    return STRIP_RE.sub("", s).lower()


def _strip_year(s: str) -> str:
    return YEAR_RE.sub("", s).strip()


def fuzzy_match(a: str, b: str, threshold: float = 0.8) -> bool:
    """Return True if ``a`` and ``b`` are a close match."""
    ratio = SequenceMatcher(None, _clean(a), _clean(b)).ratio()
    return ratio >= threshold
