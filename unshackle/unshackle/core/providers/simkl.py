from __future__ import annotations

from typing import Optional, Union

import requests

from unshackle.core.config import config
from unshackle.core.providers._base import ExternalIds, MetadataProvider, MetadataResult, fuzzy_match


class SimklProvider(MetadataProvider):
    """SIMKL metadata provider (filename-based search)."""

    NAME = "simkl"
    REQUIRES_KEY = True
    BASE_URL = "https://api.simkl.com"

    def is_available(self) -> bool:
        return bool(config.simkl_client_id)

    def search(self, title: str, year: Optional[int], kind: str) -> Optional[MetadataResult]:
        self.log.debug("Searching Simkl for %r (%s, %s)", title, kind, year)

        # Construct appropriate filename based on type
        filename = f"{title}"
        if year:
            filename = f"{title} {year}"
        if kind == "tv":
            filename += " S01E01.mkv"
        else:
            filename += " 2160p.mkv"

        try:
            headers = {"simkl-api-key": config.simkl_client_id}
            resp = self.session.post(
                f"{self.BASE_URL}/search/file", json={"file": filename}, headers=headers, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            self.log.debug("Simkl API response received")
        except (requests.RequestException, ValueError) as exc:
            self.log.debug("Simkl search failed: %s", exc)
            return None

        # Handle case where SIMKL returns empty list (no results)
        if isinstance(data, list):
            self.log.debug("Simkl returned list (no matches) for %r", filename)
            return None

        return self._parse_response(data, title, year, kind)

    def get_by_id(self, provider_id: Union[int, str], kind: str) -> Optional[MetadataResult]:
        return None  # SIMKL has no direct ID lookup used here

    def get_external_ids(self, provider_id: Union[int, str], kind: str) -> ExternalIds:
        return ExternalIds()  # IDs come from search() response

    def find_by_imdb_id(self, imdb_id: str, kind: str) -> Optional[ExternalIds]:
        """Look up TMDB/TVDB IDs from an IMDB ID using SIMKL's /search/id and detail endpoints."""
        self.log.debug("Looking up IMDB ID %s on SIMKL", imdb_id)
        headers = {"simkl-api-key": config.simkl_client_id}

        try:
            r = self.session.get(f"{self.BASE_URL}/search/id", params={"imdb": imdb_id}, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            self.log.debug("SIMKL search/id failed: %s", exc)
            return None

        if not isinstance(data, list) or not data:
            self.log.debug("No SIMKL results for IMDB ID %s", imdb_id)
            return None

        entry = data[0]
        simkl_id = entry.get("ids", {}).get("simkl")
        if not simkl_id:
            return None

        # Map SIMKL type to endpoint
        simkl_type = entry.get("type", "")
        endpoint = "tv" if simkl_type in ("tv", "anime") else "movies"

        # Fetch full details to get cross-referenced IDs
        try:
            r2 = self.session.get(
                f"{self.BASE_URL}/{endpoint}/{simkl_id}",
                params={"extended": "full"},
                headers=headers,
                timeout=30,
            )
            r2.raise_for_status()
            detail = r2.json()
        except (requests.RequestException, ValueError) as exc:
            self.log.debug("SIMKL detail fetch failed: %s", exc)
            return None

        ids = detail.get("ids", {})
        tmdb_id: Optional[int] = None
        raw_tmdb = ids.get("tmdb")
        if raw_tmdb:
            tmdb_id = int(raw_tmdb)

        tvdb_id: Optional[int] = None
        raw_tvdb = ids.get("tvdb")
        if raw_tvdb:
            tvdb_id = int(raw_tvdb)

        self.log.debug("SIMKL find -> TMDB %s, TVDB %s for IMDB %s", tmdb_id, tvdb_id, imdb_id)

        return ExternalIds(
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            tmdb_kind=kind,
            tvdb_id=tvdb_id,
        )

    def _parse_response(
        self, data: dict, search_title: str, search_year: Optional[int], kind: str
    ) -> Optional[MetadataResult]:
        """Parse a SIMKL response into a MetadataResult."""
        if data.get("type") == "episode" and "show" in data:
            info = data["show"]
            content_type = "tv"
        elif data.get("type") == "movie" and "movie" in data:
            info = data["movie"]
            content_type = "movie"
        else:
            return None

        result_title = info.get("title")
        result_year = info.get("year")

        # Verify title matches
        if not result_title or not fuzzy_match(result_title, search_title):
            self.log.debug("Simkl title mismatch: searched %r, got %r", search_title, result_title)
            return None

        # Verify year if provided (allow 1 year difference)
        if search_year and result_year and abs(search_year - result_year) > 1:
            self.log.debug("Simkl year mismatch: searched %d, got %d", search_year, result_year)
            return None

        ids = info.get("ids", {})
        tmdb_id: Optional[int] = None
        if content_type == "tv":
            raw_tmdb = ids.get("tmdbtv")
        else:
            raw_tmdb = ids.get("tmdb") or ids.get("moviedb")
        if raw_tmdb:
            tmdb_id = int(raw_tmdb)

        tvdb_id: Optional[int] = None
        raw_tvdb = ids.get("tvdb")
        if raw_tvdb:
            tvdb_id = int(raw_tvdb)

        self.log.debug("Simkl -> %s (TMDB ID %s)", result_title, tmdb_id)

        return MetadataResult(
            title=result_title,
            year=result_year,
            kind=kind,
            external_ids=ExternalIds(
                imdb_id=ids.get("imdb"),
                tmdb_id=tmdb_id,
                tmdb_kind=kind,
                tvdb_id=tvdb_id,
            ),
            source="simkl",
            raw=data,
        )
