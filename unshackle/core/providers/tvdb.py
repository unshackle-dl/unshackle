from __future__ import annotations

from collections.abc import Collection
from difflib import SequenceMatcher
from typing import Any, Optional, Union

import requests

from unshackle.core.config import config
from unshackle.core.providers._base import ExternalIds, MetadataProvider, MetadataResult, _clean, _strip_year


def primary_language(data: dict) -> Optional[str]:
    """Search results spell the key primary_language, the extended record originalLanguage."""
    return data.get("primary_language") or data.get("originalLanguage") or None


KIND_TO_TYPE: dict[str, str] = {"movie": "movie", "tv": "series"}
KIND_TO_PATH: dict[str, str] = {"movie": "movies", "tv": "series"}

# a series' Streaming Order is published as "alternate"
SEASON_TYPES: tuple[str, ...] = ("official", "dvd", "absolute", "alternate", "regional")
AIRED_ORDER = "official"

# cached per-process; providers are re-instantiated per lookup
_token: Optional[str] = None


def _parse_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ids_from_remote(remote_ids: Optional[list], tvdb_id: Optional[int]) -> ExternalIds:
    """Build ExternalIds from a TVDB remote_ids/remoteIds list."""
    ext = ExternalIds(tvdb_id=tvdb_id)
    for entry in remote_ids or []:
        source = (entry.get("sourceName") or "").lower()
        value = entry.get("id")
        if source == "imdb":
            ext.imdb_id = value
        elif source.startswith("themoviedb"):
            ext.tmdb_id = _parse_int(value)
    return ext


def _pick_match(
    results: list[dict], search_title: str, year: Optional[int]
) -> tuple[Optional[dict], Optional[str], float]:
    """Best title match among results, ignoring any whose year is more than a year out."""
    best: Optional[dict] = None
    best_title: Optional[str] = None
    best_ratio = 0.0
    for result in results:
        result_year = _parse_int(result.get("year"))
        if year and result_year and abs(result_year - year) > 1:
            continue
        for candidate in [result.get("name"), *(result.get("aliases") or [])]:
            if not candidate:
                continue
            ratio = SequenceMatcher(None, _clean(search_title), _clean(candidate)).ratio()
            if ratio > best_ratio:
                best, best_title, best_ratio = result, candidate, ratio
    return best, best_title, best_ratio


class TVDBProvider(MetadataProvider):
    """TheTVDB v4 metadata provider. Native IDs are TVDB IDs."""

    NAME = "tvdb"
    REQUIRES_KEY = True
    BASE_URL = "https://api4.thetvdb.com/v4"

    def __init__(self) -> None:
        super().__init__()
        self._episodes: dict[tuple[int, str], list[dict]] = {}

    def is_available(self) -> bool:
        return bool(config.tvdb_api_key)

    def _login(self) -> Optional[str]:
        global _token
        payload: dict[str, str] = {"apikey": config.tvdb_api_key}
        if config.tvdb_pin:
            payload["pin"] = config.tvdb_pin
        try:
            r = self.session.post(f"{self.BASE_URL}/login", json=payload, timeout=30)
            r.raise_for_status()
            _token = r.json()["data"]["token"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            self.log.warning("Failed to authenticate with TVDB: %s", exc)
            _token = None
        return _token

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        """The `data` payload of a TVDB response: a dict for entities, a list for searches."""
        global _token
        token = _token or self._login()
        if not token:
            return None
        for _ in range(2):
            try:
                r = self.session.get(
                    f"{self.BASE_URL}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30,
                )
                if r.status_code == 401:
                    _token = None
                    token = self._login()
                    if not token:
                        return None
                    continue
                r.raise_for_status()
                return r.json().get("data")
            except (requests.RequestException, ValueError) as exc:
                self.log.debug("TVDB request %s failed: %s", path, exc)
                return None
        return None

    def search(self, title: str, year: Optional[int], kind: str) -> Optional[MetadataResult]:
        search_title = _strip_year(title)
        self.log.debug("Searching TVDB for %r (%s, %s)", search_title, kind, year)

        params: dict[str, str | int] = {"query": search_title, "limit": 10}
        entity_type = KIND_TO_TYPE.get(kind)
        if entity_type:
            params["type"] = entity_type
        if year is not None:
            params["year"] = year

        results = self._get("/search", params)
        if not results and year is not None:
            del params["year"]
            results = self._get("/search", params)
        if not results:
            self.log.debug("No TVDB results for %r", search_title)
            return None

        # year first, so an identically named remake cannot win on title alone
        best, best_title, best_ratio = _pick_match(results, search_title, year)
        if best is None:
            best, best_title, best_ratio = _pick_match(results, search_title, None)
        if best is None:
            best, best_title = results[0], results[0].get("name")

        tvdb_id = _parse_int(best.get("tvdb_id"))
        ext = _ids_from_remote(best.get("remote_ids"), tvdb_id)
        if ext.tmdb_id:
            ext.tmdb_kind = kind

        self.log.debug("TVDB -> %r (ID %s, ratio %.2f)", best_title, tvdb_id, best_ratio)

        return MetadataResult(
            title=best_title or best.get("name"),
            year=_parse_int(best.get("year")),
            kind=kind,
            external_ids=ext,
            original_language=primary_language(best),
            source="tvdb",
            raw=best,
        )

    def _fetch_extended(self, tvdb_id: int, kind: str) -> Optional[dict]:
        path = KIND_TO_PATH.get(kind, "series")
        return self._get(f"/{path}/{tvdb_id}/extended", {"short": "true"})

    def get_by_id(self, provider_id: Union[int, str], kind: str) -> Optional[MetadataResult]:
        tvdb_id = int(provider_id)
        detail = self._fetch_extended(tvdb_id, kind)
        if not detail:
            return None

        ext = _ids_from_remote(detail.get("remoteIds"), tvdb_id)
        if ext.tmdb_id:
            ext.tmdb_kind = kind

        return MetadataResult(
            title=detail.get("name"),
            year=_parse_int(detail.get("year")),
            kind=kind,
            external_ids=ext,
            original_language=primary_language(detail),
            source="tvdb",
            raw=detail,
        )

    def get_external_ids(self, provider_id: Union[int, str], kind: str) -> ExternalIds:
        tvdb_id = int(provider_id)
        detail = self._fetch_extended(tvdb_id, kind)
        if not detail:
            return ExternalIds(tvdb_id=tvdb_id)
        ext = _ids_from_remote(detail.get("remoteIds"), tvdb_id)
        if ext.tmdb_id:
            ext.tmdb_kind = kind
        return ext

    def get_episodes(self, tvdb_id: int, order: str) -> list[dict]:
        """All episodes of a series in the given season order."""
        cached = self._episodes.get((tvdb_id, order))
        if cached is not None:
            return cached

        episodes: list[dict] = []
        for page in range(20):  # ponytail: hard page cap, 500 eps/page covers any real series
            data = self._get(f"/series/{tvdb_id}/episodes/{order}", {"page": page})
            if data is None and page:
                # a partial listing would renumber episodes wrongly
                self.log.warning("TVDB %s order listing for series %s failed at page %d", order, tvdb_id, page)
                return []
            batch = (data or {}).get("episodes") or []
            episodes.extend(batch)
            if len(batch) < 500:
                break

        if episodes:  # an empty listing may be a transient failure
            self._episodes[(tvdb_id, order)] = episodes
        return episodes

    def detect_order(self, tvdb_id: int, keys: Collection[tuple[int, int]]) -> str:
        """Guess which TVDB season order a service's (season, episode) numbering follows.

        A service does not always number a series in aired order, so the numbering is
        scored against every order rather than assumed.
        """
        best, best_score = AIRED_ORDER, -1
        for order in SEASON_TYPES:
            episodes = self.get_episodes(tvdb_id, order)
            if not episodes:
                continue
            slots = {(_parse_int(e.get("seasonNumber")), _parse_int(e.get("number"))) for e in episodes}
            score = sum(1 for key in keys if key in slots)
            self.log.debug("TVDB order %s matches %d/%d of the service's episodes", order, score, len(keys))
            if score > best_score:
                best, best_score = order, score
            if order == AIRED_ORDER and score == len(keys):
                break  # aired order already accounts for every episode
        return best

    def get_order_map(
        self, tvdb_id: int, order: str, source_order: str = AIRED_ORDER
    ) -> dict[tuple[int, int], tuple[int, int, Optional[str]]]:
        """Map (season, episode) in `source_order` to (season, episode, name) in `order`.

        Every order lists the same episodes under the same TVDB episode ID, which is what
        the two listings join on.
        """
        if order == source_order:
            return {}

        source = self.get_episodes(tvdb_id, source_order)
        target = self.get_episodes(tvdb_id, order)
        if not source or not target:
            self.log.warning("TVDB has no %s order for series %s", order, tvdb_id)
            return {}

        by_id = {ep.get("id"): ep for ep in target}
        mapping: dict[tuple[int, int], tuple[int, int, Optional[str]]] = {}
        for ep in source:
            match = by_id.get(ep.get("id"))
            if not match:
                continue
            src_season, src_number = _parse_int(ep.get("seasonNumber")), _parse_int(ep.get("number"))
            dst_season, dst_number = _parse_int(match.get("seasonNumber")), _parse_int(match.get("number"))
            if src_season is None or src_number is None or dst_season is None or dst_number is None:
                continue
            mapping[(src_season, src_number)] = (dst_season, dst_number, match.get("name"))

        self.log.debug("TVDB %s -> %s order: mapped %d/%d episodes", source_order, order, len(mapping), len(source))
        return mapping

    def find_by_imdb_id(self, imdb_id: str, kind: str) -> Optional[ExternalIds]:
        """Resolve a TVDB ID from an IMDb ID via /search/remoteid."""
        results = self._get(f"/search/remoteid/{imdb_id}")
        if not results:
            self.log.debug("No TVDB results for IMDB ID %s", imdb_id)
            return None

        # each hit wraps a single entity, e.g. {"series": {...}} or {"movie": {...}}
        preferred = KIND_TO_TYPE.get(kind, "series")
        entity = None
        for hit in results:
            entity = hit.get(preferred) or entity or next(iter(hit.values()), None)
            if hit.get(preferred):
                break
        if not entity:
            return None

        tvdb_id = _parse_int(entity.get("id"))
        self.log.debug("TVDB find -> TVDB %s for IMDB %s", tvdb_id, imdb_id)
        return ExternalIds(imdb_id=imdb_id, tvdb_id=tvdb_id)
