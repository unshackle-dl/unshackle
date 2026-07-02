from __future__ import annotations

from difflib import SequenceMatcher
from typing import Optional, Union

import requests

from unshackle.core.providers._base import ExternalIds, MetadataProvider, MetadataResult, _clean, fuzzy_match

# Mapping from our kind ("movie"/"tv") to imdbapi.dev title types
KIND_TO_TYPES: dict[str, list[str]] = {
    "movie": ["movie"],
    "tv": ["tvSeries", "tvMiniSeries"],
}


class IMDBApiProvider(MetadataProvider):
    """IMDb metadata provider using imdbapi.dev (free, no API key)."""

    NAME = "imdbapi"
    REQUIRES_KEY = False
    BASE_URL = "https://api.imdbapi.dev"

    def is_available(self) -> bool:
        return True  # no key needed

    def search(self, title: str, year: Optional[int], kind: str) -> Optional[MetadataResult]:
        self.log.debug("Searching IMDBApi for %r (%s, %s)", title, kind, year)

        try:
            params: dict[str, str | int] = {"query": title, "limit": 20}
            r = self.session.get(
                f"{self.BASE_URL}/search/titles",
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            self.log.debug("IMDBApi search failed: %s", exc)
            return None

        results = data.get("titles") or data.get("results") or []
        if not results:
            self.log.debug("IMDBApi returned no results for %r", title)
            return None

        # Filter by type if possible
        type_filter = KIND_TO_TYPES.get(kind, [])
        filtered = [r for r in results if r.get("type") in type_filter] if type_filter else results
        candidates = filtered if filtered else results

        # Find best fuzzy match, optionally filtered by year
        best_match: Optional[dict] = None
        best_ratio = 0.0

        for candidate in candidates:
            primary = candidate.get("primaryTitle") or ""
            original = candidate.get("originalTitle") or ""

            for name in [primary, original]:
                if not name:
                    continue
                ratio = SequenceMatcher(None, _clean(title), _clean(name)).ratio()
                if ratio > best_ratio:
                    # If year provided, prefer matches within 1 year
                    candidate_year = candidate.get("startYear")
                    if year and candidate_year and abs(year - candidate_year) > 1:
                        continue
                    best_ratio = ratio
                    best_match = candidate

        if not best_match:
            self.log.debug("No matching result found in IMDBApi for %r", title)
            return None

        result_title = best_match.get("primaryTitle") or best_match.get("originalTitle")
        if not result_title or not fuzzy_match(result_title, title):
            self.log.debug("IMDBApi title mismatch: searched %r, got %r", title, result_title)
            return None

        imdb_id = best_match.get("id")
        result_year = best_match.get("startYear")

        self.log.debug("IMDBApi -> %s (ID %s)", result_title, imdb_id)

        return MetadataResult(
            title=result_title,
            year=result_year,
            kind=kind,
            external_ids=ExternalIds(imdb_id=imdb_id),
            source="imdbapi",
            raw=best_match,
        )

    def get_by_id(self, provider_id: Union[int, str], kind: str) -> Optional[MetadataResult]:
        """Fetch metadata by IMDB ID (e.g. 'tt1375666')."""
        imdb_id = str(provider_id)
        self.log.debug("Fetching IMDBApi title %s", imdb_id)

        try:
            r = self.session.get(f"{self.BASE_URL}/titles/{imdb_id}", timeout=30)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            self.log.debug("IMDBApi get_by_id failed: %s", exc)
            return None

        title = data.get("primaryTitle") or data.get("originalTitle")
        result_year = data.get("startYear")

        return MetadataResult(
            title=title,
            year=result_year,
            kind=kind,
            external_ids=ExternalIds(imdb_id=data.get("id")),
            source="imdbapi",
            raw=data,
        )

    def get_external_ids(self, provider_id: Union[int, str], kind: str) -> ExternalIds:
        """Return external IDs. For IMDB, the provider_id IS the IMDB ID."""
        return ExternalIds(imdb_id=str(provider_id))
