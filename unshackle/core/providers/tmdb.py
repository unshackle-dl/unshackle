from __future__ import annotations

from difflib import SequenceMatcher
from typing import Optional, Union

import requests

from unshackle.core.config import config
from unshackle.core.providers._base import ExternalIds, MetadataProvider, MetadataResult, _clean, _strip_year


class TMDBProvider(MetadataProvider):
    """TMDB (The Movie Database) metadata provider."""

    NAME = "tmdb"
    REQUIRES_KEY = True
    BASE_URL = "https://api.themoviedb.org/3"

    def is_available(self) -> bool:
        return bool(config.tmdb_api_key)

    @property
    def _api_key(self) -> str:
        return config.tmdb_api_key

    def search(self, title: str, year: Optional[int], kind: str) -> Optional[MetadataResult]:
        search_title = _strip_year(title)
        self.log.debug("Searching TMDB for %r (%s, %s)", search_title, kind, year)

        params: dict[str, str | int] = {"api_key": self._api_key, "query": search_title}
        if year is not None:
            params["year" if kind == "movie" else "first_air_date_year"] = year

        try:
            r = self.session.get(f"{self.BASE_URL}/search/{kind}", params=params, timeout=30)
            r.raise_for_status()
            results = r.json().get("results") or []
            self.log.debug("TMDB returned %d results", len(results))
            if not results:
                return None
        except requests.RequestException as exc:
            self.log.warning("Failed to search TMDB for %s: %s", title, exc)
            return None

        best_ratio = 0.0
        best_id: Optional[int] = None
        best_title: Optional[str] = None
        for result in results:
            candidates = [
                result.get("title"),
                result.get("name"),
                result.get("original_title"),
                result.get("original_name"),
            ]
            candidates = [c for c in candidates if c]

            for candidate in candidates:
                ratio = SequenceMatcher(None, _clean(search_title), _clean(candidate)).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_id = result.get("id")
                    best_title = candidate

        self.log.debug("Best candidate ratio %.2f for %r (ID %s)", best_ratio, best_title, best_id)

        if best_id is None:
            first = results[0]
            best_id = first.get("id")
            best_title = first.get("title") or first.get("name")

        if best_id is None:
            return None

        # Fetch full detail for caching
        detail = self._fetch_detail(best_id, kind)
        ext_raw = self._fetch_external_ids_raw(best_id, kind)

        date = (detail or {}).get("release_date") or (detail or {}).get("first_air_date")
        result_year = int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None

        ext = ExternalIds(
            imdb_id=ext_raw.get("imdb_id") if ext_raw else None,
            tmdb_id=best_id,
            tmdb_kind=kind,
            tvdb_id=ext_raw.get("tvdb_id") if ext_raw else None,
        )

        return MetadataResult(
            title=best_title,
            year=result_year,
            kind=kind,
            external_ids=ext,
            source="tmdb",
            raw={"detail": detail or {}, "external_ids": ext_raw or {}},
        )

    def get_by_id(self, provider_id: Union[int, str], kind: str) -> Optional[MetadataResult]:
        detail = self._fetch_detail(int(provider_id), kind)
        if not detail:
            return None

        title = detail.get("title") or detail.get("name")
        date = detail.get("release_date") or detail.get("first_air_date")
        year = int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None

        return MetadataResult(
            title=title,
            year=year,
            kind=kind,
            external_ids=ExternalIds(tmdb_id=int(provider_id), tmdb_kind=kind),
            source="tmdb",
            raw=detail,
        )

    def get_external_ids(self, provider_id: Union[int, str], kind: str) -> ExternalIds:
        raw = self._fetch_external_ids_raw(int(provider_id), kind)
        if not raw:
            return ExternalIds(tmdb_id=int(provider_id), tmdb_kind=kind)
        return ExternalIds(
            imdb_id=raw.get("imdb_id"),
            tmdb_id=int(provider_id),
            tmdb_kind=kind,
            tvdb_id=raw.get("tvdb_id"),
        )

    def find_by_imdb_id(self, imdb_id: str, kind: str) -> Optional[ExternalIds]:
        """Look up TMDB/TVDB IDs from an IMDB ID using TMDB's /find endpoint."""
        self.log.debug("Looking up IMDB ID %s on TMDB", imdb_id)
        try:
            r = self.session.get(
                f"{self.BASE_URL}/find/{imdb_id}",
                params={"api_key": self._api_key, "external_source": "imdb_id"},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as exc:
            self.log.debug("TMDB find by IMDB ID failed: %s", exc)
            return None

        # Check movie_results or tv_results based on kind
        if kind == "movie":
            results = data.get("movie_results") or []
        else:
            results = data.get("tv_results") or []

        if not results:
            # Try the other type as fallback
            fallback_key = "tv_results" if kind == "movie" else "movie_results"
            results = data.get(fallback_key) or []
            if results:
                kind = "tv" if kind == "movie" else "movie"

        if not results:
            self.log.debug("No TMDB results found for IMDB ID %s", imdb_id)
            return None

        match = results[0]
        tmdb_id = match.get("id")
        if not tmdb_id:
            return None

        self.log.debug("TMDB find -> ID %s (%s) for IMDB %s", tmdb_id, kind, imdb_id)

        # Now fetch the full external IDs from TMDB to get TVDB etc.
        ext_raw = self._fetch_external_ids_raw(tmdb_id, kind)

        return ExternalIds(
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            tmdb_kind=kind,
            tvdb_id=ext_raw.get("tvdb_id") if ext_raw else None,
        )

    def _fetch_detail(self, tmdb_id: int, kind: str) -> Optional[dict]:
        try:
            r = self.session.get(
                f"{self.BASE_URL}/{kind}/{tmdb_id}",
                params={"api_key": self._api_key},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            self.log.debug("Failed to fetch TMDB detail: %s", exc)
            return None

    def _fetch_external_ids_raw(self, tmdb_id: int, kind: str) -> Optional[dict]:
        try:
            r = self.session.get(
                f"{self.BASE_URL}/{kind}/{tmdb_id}/external_ids",
                params={"api_key": self._api_key},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            self.log.debug("Failed to fetch TMDB external IDs: %s", exc)
            return None
