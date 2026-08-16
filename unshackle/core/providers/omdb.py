from __future__ import annotations

from difflib import SequenceMatcher
from typing import Optional, Union

import requests
from langcodes import Language

from unshackle.core.config import config
from unshackle.core.providers._base import ExternalIds, MetadataProvider, MetadataResult, clean, fuzzy_match

# Mapping from our kind ("movie"/"tv") to OMDb title types
KIND_TO_TYPE: dict[str, str] = {
    "movie": "movie",
    "tv": "series",
}


def primary_language(data: dict) -> Optional[str]:
    """OMDb gives English language names, most prominent first, e.g. 'Korean, English'."""
    name = (data.get("Language") or "").split(",")[0].strip()
    if not name:
        return None
    try:
        return str(Language.find(name))
    except LookupError:
        return None


def parse_year(value: Optional[str]) -> Optional[int]:
    # OMDb years look like "2017", "2008–2013" or "2023–"
    if value and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return None


class OMDBProvider(MetadataProvider):
    """OMDb metadata provider (omdbapi.com). Native IDs are IMDb IDs."""

    NAME = "omdb"
    REQUIRES_KEY = True
    ID_KIND = "imdb"
    BASE_URL = "https://www.omdbapi.com/"

    def is_available(self) -> bool:
        return bool(config.omdb_api_key)

    @property
    def api_key(self) -> str:
        return config.omdb_api_key

    def api_get(self, params: dict[str, str]) -> Optional[dict]:
        try:
            r = self.session.get(self.BASE_URL, params={"apikey": self.api_key, **params}, timeout=30)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            self.log.debug("OMDb request failed: %s", exc)
            return None
        if data.get("Response") != "True":
            self.log.debug("OMDb error: %s", data.get("Error"))
            return None
        return data

    def search(self, title: str, year: Optional[int], kind: str) -> Optional[MetadataResult]:
        self.log.debug("Searching OMDb for %r (%s, %s)", title, kind, year)

        params: dict[str, str] = {"s": title}
        type_filter = KIND_TO_TYPE.get(kind)
        if type_filter:
            params["type"] = type_filter
        if year is not None:
            params["y"] = str(year)

        data = self.api_get(params)
        if not data and year is not None:
            # OMDb's year filter is exact; retry without it for off-by-one metadata
            del params["y"]
            data = self.api_get(params)
        if not data:
            return None

        results = data.get("Search") or []

        best_match: Optional[dict] = None
        best_ratio = 0.0
        for candidate in results:
            name = candidate.get("Title") or ""
            if not name:
                continue
            ratio = SequenceMatcher(None, clean(title), clean(name)).ratio()
            if ratio > best_ratio:
                candidate_year = parse_year(candidate.get("Year"))
                if year and candidate_year and abs(year - candidate_year) > 1:
                    continue
                best_ratio = ratio
                best_match = candidate

        if not best_match:
            self.log.debug("No matching result found in OMDb for %r", title)
            return None

        result_title = best_match.get("Title")
        if not result_title or not fuzzy_match(result_title, title):
            self.log.debug("OMDb title mismatch: searched %r, got %r", title, result_title)
            return None

        imdb_id = best_match.get("imdbID")
        self.log.debug("OMDb -> %s (ID %s)", result_title, imdb_id)

        # Fetch full detail so raw carries ratings, genre, rating cert, etc.
        detail = self.api_get({"i": imdb_id}) if imdb_id else None

        return MetadataResult(
            title=result_title,
            year=parse_year(best_match.get("Year")),
            kind=kind,
            external_ids=ExternalIds(imdb_id=imdb_id),
            original_language=primary_language(detail or best_match),
            source="omdb",
            raw=detail or best_match,
        )

    def get_by_id(self, provider_id: Union[int, str], kind: str) -> Optional[MetadataResult]:
        """Fetch metadata by IMDb ID (e.g. 'tt1375666')."""
        imdb_id = str(provider_id)
        self.log.debug("Fetching OMDb title %s", imdb_id)

        data = self.api_get({"i": imdb_id})
        if not data:
            return None

        return MetadataResult(
            title=data.get("Title"),
            year=parse_year(data.get("Year")),
            kind=kind,
            external_ids=ExternalIds(imdb_id=data.get("imdbID")),
            original_language=primary_language(data),
            source="omdb",
            raw=data,
        )

    def get_external_ids(self, provider_id: Union[int, str], kind: str) -> ExternalIds:
        """Return external IDs. For OMDb, the provider_id IS the IMDb ID."""
        return ExternalIds(imdb_id=str(provider_id))
