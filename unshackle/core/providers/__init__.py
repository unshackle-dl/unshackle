from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union

import requests

from unshackle.core.providers._base import ExternalIds, MetadataProvider, MetadataResult, fuzzy_match, log
from unshackle.core.providers.anilist import AniListProvider, parse_anilist_ref
from unshackle.core.providers.imdb import IMDBProvider
from unshackle.core.providers.omdb import OMDBProvider
from unshackle.core.providers.simkl import SimklProvider
from unshackle.core.providers.tmdb import TMDBProvider
from unshackle.core.providers.tvdb import TVDBProvider

if TYPE_CHECKING:
    from unshackle.core.title_cacher import TitleCacher

REGISTRY: dict[str, type[MetadataProvider]] = {
    cls.NAME: cls for cls in (AniListProvider, IMDBProvider, OMDBProvider, SimklProvider, TMDBProvider, TVDBProvider)
}

# legacy `metadata_providers` names, still accepted
ALIASES: dict[str, str] = {"imdbapi": "imdb"}

# used when `metadata_providers` is unset; anilist answers for anime only, so it costs
# nothing at the end of the order
DEFAULT_ORDER: tuple[str, ...] = ("imdb", "omdb", "simkl", "tmdb", "tvdb", "anilist")


def provider_order(kind: Optional[str] = None, anime: bool = False) -> list[type[MetadataProvider]]:
    """Provider classes in the order configured by `metadata_providers`.

    `metadata_providers` is either a flat list applying to both kinds, or a mapping of
    kind ("tv"/"movie") to its own list. A kind the mapping omits uses `DEFAULT_ORDER`.
    An `anime` title puts anilist first; the rest of the order stays behind it to
    fall back on.
    """
    from unshackle.core.config import config

    configured = config.metadata_providers
    selected = (configured.get(kind) if kind else None) if isinstance(configured, dict) else configured

    names = [ALIASES.get(str(n).lower(), str(n).lower()) for n in (selected or DEFAULT_ORDER)]
    if anime and AniListProvider.NAME in names:
        names = [AniListProvider.NAME, *names]
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        log.warning("Ignoring unknown metadata_providers entries: %s", ", ".join(unknown))
    return [REGISTRY[n] for n in dict.fromkeys(names) if n in REGISTRY]


def get_available_providers() -> list[MetadataProvider]:
    """Return instantiated providers that have valid credentials."""
    return [cls() for cls in provider_order() if cls().is_available()]


def get_provider(name: str) -> Optional[MetadataProvider]:
    """Get a specific provider by name."""
    cls = REGISTRY.get(name)
    if not cls:
        return None
    p = cls()
    return p if p.is_available() else None


# -- Public API (replaces tags.py functions) --


def search_metadata(
    title: str,
    year: Optional[int],
    kind: str,
    title_cacher: Optional[TitleCacher] = None,
    cache_title_id: Optional[str] = None,
    cache_region: Optional[str] = None,
    cache_account_hash: Optional[str] = None,
    anime: bool = False,
) -> Optional[MetadataResult]:
    """Search all available providers for metadata. Returns best match."""
    from unshackle.core.config import config

    # the one gate for `disable_metadata`: every automatic lookup reaches a provider through
    # here, while a user-supplied ID goes straight to `resolve_by_ids` and stays allowed
    if config.disable_metadata:
        log.debug("Metadata lookups are disabled by config; not searching for %r", title)
        return None

    ordered = provider_order(kind, anime)

    # Check cache first
    if title_cacher and cache_title_id:
        for cls in ordered:
            p = cls()
            if not p.is_available():
                continue
            cached = title_cacher.get_cached_provider(p.NAME, cache_title_id, kind, cache_region, cache_account_hash)
            if cached:
                result = _cached_to_result(cached, p.NAME, kind)
                if result and result.title and fuzzy_match(result.title, title):
                    log.debug("Using cached %s data for %r", p.NAME, title)
                    return result

    # Search providers in priority order
    for cls in ordered:
        p = cls()
        if not p.is_available():
            continue
        try:
            result = p.search(title, year, kind)
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.debug("%s search failed: %s", p.NAME, exc)
            continue
        if result and result.title and fuzzy_match(result.title, title):
            # Enrich with cross-referenced IDs if we have IMDB but missing TMDB/TVDB
            enrich_ids(result)
            # Cache the result (include enriched IDs so they survive round-trip)
            if title_cacher and cache_title_id and result.raw:
                try:
                    cache_data = result.raw
                    if result.external_ids.tmdb_id or result.external_ids.tvdb_id:
                        cache_data = {
                            **result.raw,
                            "_enriched_ids": _external_ids_to_dict(result.external_ids),
                        }
                    title_cacher.cache_provider(
                        p.NAME, cache_title_id, cache_data, kind, cache_region, cache_account_hash
                    )
                except Exception as exc:
                    log.debug("Failed to cache %s data: %s", p.NAME, exc)
            return result

    return None


def get_title_by_id(
    tmdb_id: int,
    kind: str,
    title_cacher: Optional[TitleCacher] = None,
    cache_title_id: Optional[str] = None,
    cache_region: Optional[str] = None,
    cache_account_hash: Optional[str] = None,
) -> Optional[str]:
    """Get title name by TMDB ID."""
    # Check cache first
    if title_cacher and cache_title_id:
        cached = title_cacher.get_cached_provider("tmdb", cache_title_id, kind, cache_region, cache_account_hash)
        if cached and cached.get("detail"):
            detail = cached["detail"]
            tmdb_title = detail.get("title") or detail.get("name")
            if tmdb_title:
                log.debug("Using cached TMDB title: %r", tmdb_title)
                return tmdb_title

    tmdb = get_provider("tmdb")
    if not tmdb:
        return None
    result = tmdb.get_by_id(tmdb_id, kind)
    if not result:
        return None

    # Cache if possible
    if title_cacher and cache_title_id and result.raw:
        try:
            ext_ids = tmdb.get_external_ids(tmdb_id, kind)
            title_cacher.cache_provider(
                "tmdb",
                cache_title_id,
                {"detail": result.raw, "external_ids": _external_ids_to_dict(ext_ids)},
                kind,
                cache_region,
                cache_account_hash,
            )
        except Exception as exc:
            log.debug("Failed to cache TMDB data: %s", exc)

    return result.title


def get_year_by_id(
    tmdb_id: int,
    kind: str,
    title_cacher: Optional[TitleCacher] = None,
    cache_title_id: Optional[str] = None,
    cache_region: Optional[str] = None,
    cache_account_hash: Optional[str] = None,
) -> Optional[int]:
    """Get release year by TMDB ID."""
    # Check cache first
    if title_cacher and cache_title_id:
        cached = title_cacher.get_cached_provider("tmdb", cache_title_id, kind, cache_region, cache_account_hash)
        if cached and cached.get("detail"):
            detail = cached["detail"]
            date = detail.get("release_date") or detail.get("first_air_date")
            if date and len(date) >= 4 and date[:4].isdigit():
                year = int(date[:4])
                log.debug("Using cached TMDB year: %d", year)
                return year

    tmdb = get_provider("tmdb")
    if not tmdb:
        return None
    result = tmdb.get_by_id(tmdb_id, kind)
    if not result:
        return None

    # Cache if possible
    if title_cacher and cache_title_id and result.raw:
        try:
            ext_ids = tmdb.get_external_ids(tmdb_id, kind)
            title_cacher.cache_provider(
                "tmdb",
                cache_title_id,
                {"detail": result.raw, "external_ids": _external_ids_to_dict(ext_ids)},
                kind,
                cache_region,
                cache_account_hash,
            )
        except Exception as exc:
            log.debug("Failed to cache TMDB data: %s", exc)

    return result.year


def get_language_by_id(
    tmdb_id: int,
    kind: str,
    title_cacher: Optional[TitleCacher] = None,
    cache_title_id: Optional[str] = None,
    cache_region: Optional[str] = None,
    cache_account_hash: Optional[str] = None,
) -> Optional[str]:
    """Get original language by TMDB ID."""
    if title_cacher and cache_title_id:
        cached = title_cacher.get_cached_provider("tmdb", cache_title_id, kind, cache_region, cache_account_hash)
        language = ((cached or {}).get("detail") or {}).get("original_language")
        if language:
            log.debug("Using cached TMDB original language: %s", language)
            return language

    tmdb = get_provider("tmdb")
    if not tmdb:
        return None
    result = tmdb.get_by_id(tmdb_id, kind)
    return result.original_language if result else None


def fetch_external_ids(
    tmdb_id: int,
    kind: str,
    title_cacher: Optional[TitleCacher] = None,
    cache_title_id: Optional[str] = None,
    cache_region: Optional[str] = None,
    cache_account_hash: Optional[str] = None,
) -> ExternalIds:
    """Get external IDs by TMDB ID."""
    # Check cache first
    if title_cacher and cache_title_id:
        cached = title_cacher.get_cached_provider("tmdb", cache_title_id, kind, cache_region, cache_account_hash)
        if cached and cached.get("external_ids"):
            log.debug("Using cached TMDB external IDs")
            raw = cached["external_ids"]
            return ExternalIds(
                imdb_id=raw.get("imdb_id"),
                tmdb_id=tmdb_id,
                tmdb_kind=kind,
                tvdb_id=raw.get("tvdb_id"),
            )

    tmdb = get_provider("tmdb")
    if not tmdb:
        return ExternalIds()
    ext = tmdb.get_external_ids(tmdb_id, kind)

    # Cache if possible
    if title_cacher and cache_title_id:
        try:
            detail = None
            result = tmdb.get_by_id(tmdb_id, kind)
            if result and result.raw:
                detail = result.raw
            if detail:
                title_cacher.cache_provider(
                    "tmdb",
                    cache_title_id,
                    {"detail": detail, "external_ids": _external_ids_to_dict(ext)},
                    kind,
                    cache_region,
                    cache_account_hash,
                )
        except Exception as exc:
            log.debug("Failed to cache TMDB data: %s", exc)

    return ext


def resolve_by_ids(
    tmdb_id: Optional[int] = None,
    imdb_id: Optional[str] = None,
    tvdb_id: Optional[int] = None,
    anilist_id: Optional[Union[int, str]] = None,
    *,
    title: Optional[str] = None,
    year: Optional[int] = None,
    kind: str = "movie",
    title_cacher: Optional[TitleCacher] = None,
    cache_title_id: Optional[str] = None,
    cache_region: Optional[str] = None,
    cache_account_hash: Optional[str] = None,
    anime: bool = False,
) -> Optional[MetadataResult]:
    """Resolve metadata from user-supplied external IDs, falling back to search only without them.

    A supplied ID is authoritative. It is looked up directly through the providers that
    consume its namespace, in `metadata_providers` order for `kind`, and it always survives
    into `external_ids` whatever a provider answers. A fuzzy title search only runs when no
    ID was supplied at all.
    """
    supplied: dict[str, Union[int, str]] = {}
    if tmdb_id is not None:
        supplied["tmdb"] = tmdb_id
    if imdb_id:
        supplied["imdb"] = imdb_id
    if tvdb_id is not None:
        supplied["tvdb"] = tvdb_id
    if anilist_id is not None:
        supplied["anilist"] = anilist_id

    if not supplied:
        if not title:
            return None
        return search_metadata(title, year, kind, title_cacher, cache_title_id, cache_region, cache_account_hash, anime)

    result: Optional[MetadataResult] = None
    for cls in provider_order(kind, anime):
        provider_id = supplied.get(cls.ID_KIND or "")
        if provider_id is None:
            continue
        p = get_provider(cls.NAME)
        if not p:
            continue
        try:
            result = p.get_by_id(provider_id, kind)
        except Exception as exc:
            log.debug("%s lookup of %s failed: %s", cls.NAME, provider_id, exc)
            continue
        if result:
            break

    if not result:
        result = MetadataResult(title=title, year=year, kind=kind)

    ids = result.external_ids
    if tmdb_id is not None:
        ids.tmdb_id = tmdb_id
        ids.tmdb_kind = kind
    if imdb_id:
        ids.imdb_id = imdb_id
    if tvdb_id is not None:
        ids.tvdb_id = tvdb_id
    if anilist_id is not None and ids.anilist_id is None:
        ref = parse_anilist_ref(anilist_id)
        if ref and ref[0] == "id":
            ids.anilist_id = ref[1]

    if ids.tmdb_id and not (ids.imdb_id and ids.tvdb_id):
        ext = fetch_external_ids(ids.tmdb_id, kind, title_cacher, cache_title_id, cache_region, cache_account_hash)
        if ext.imdb_id and not ids.imdb_id:
            ids.imdb_id = ext.imdb_id
        if ext.tvdb_id and not ids.tvdb_id:
            ids.tvdb_id = ext.tvdb_id
    enrich_ids(result)

    return result


# -- Internal helpers --


# trust ranking for cross-validating enrichments; `metadata_providers` filters this
# set but sets search order only, not the ranking
_ENRICHMENT_AUTHORITY: tuple[str, ...] = ("tmdb", "simkl", "tvdb")


def _enrichment_providers(kind: Optional[str] = None) -> list[str]:
    """Names of configured providers that can resolve an IMDB ID, most trusted first."""
    configured = {cls.NAME for cls in provider_order(kind) if hasattr(cls, "find_by_imdb_id")}
    return [name for name in _ENRICHMENT_AUTHORITY if name in configured]


def enrich_ids(result: MetadataResult) -> None:
    """Enrich a MetadataResult by cross-referencing IMDB ID with available providers.

    Queries all available providers, cross-validates tmdb_id as anchor.
    If a provider returns a different tmdb_id than the authoritative source,
    ALL of that provider's data is dropped (likely resolved to wrong title).
    """
    ids = result.external_ids
    if not ids.imdb_id:
        return
    if ids.tmdb_id and ids.tvdb_id:
        return  # already have everything

    kind = result.kind or "movie"

    # Step 1: Collect enrichment results from all available providers
    authority = {name: i for i, name in enumerate(_enrichment_providers(kind))}
    enrichments: list[tuple[str, ExternalIds]] = []
    for provider_name in authority:
        p = get_provider(provider_name)
        if not p:
            continue
        try:
            enriched = p.find_by_imdb_id(ids.imdb_id, kind)  # type: ignore[attr-defined]
        except Exception as exc:
            log.debug("Enrichment via %s failed: %s", provider_name, exc)
            continue
        if enriched:
            enrichments.append((provider_name, enriched))

    if not enrichments:
        return

    # Step 2: Cross-validate using tmdb_id as anchor — drop providers that disagree
    validated = _validate_enrichments(enrichments, authority)

    # Step 3: Merge validated data (fill gaps only)
    for _provider_name, ext in validated:
        if not ids.tmdb_id and ext.tmdb_id:
            ids.tmdb_id = ext.tmdb_id
            ids.tmdb_kind = ext.tmdb_kind or kind
        if not ids.tvdb_id and ext.tvdb_id:
            ids.tvdb_id = ext.tvdb_id


def _validate_enrichments(
    enrichments: list[tuple[str, ExternalIds]],
    authority: dict[str, int],
) -> list[tuple[str, ExternalIds]]:
    """Drop providers whose tmdb_id conflicts with the authoritative value.

    If providers disagree on tmdb_id, the more authoritative source wins
    and ALL data from disagreeing providers is discarded (different tmdb_id
    means the provider likely resolved to a different title entirely).
    """
    from collections import Counter

    # Collect tmdb_id votes
    tmdb_votes: dict[str, int] = {}
    for provider_name, ext in enrichments:
        if ext.tmdb_id is not None:
            tmdb_votes[provider_name] = ext.tmdb_id

    if len(set(tmdb_votes.values())) <= 1:
        return enrichments  # all agree or only one voted — no conflict

    # Find the authoritative tmdb_id
    value_counts = Counter(tmdb_votes.values())
    most_common_val, most_common_count = value_counts.most_common(1)[0]

    if most_common_count > 1:
        anchor_tmdb_id = most_common_val
    else:
        # No majority — pick the most authoritative provider
        best_provider = min(
            tmdb_votes.keys(),
            key=lambda name: authority.get(name, 99),
        )
        anchor_tmdb_id = tmdb_votes[best_provider]

    # Drop any provider that disagrees
    validated: list[tuple[str, ExternalIds]] = []
    for provider_name, ext in enrichments:
        if ext.tmdb_id is not None and ext.tmdb_id != anchor_tmdb_id:
            log.debug(
                "Dropping %s enrichment data: tmdb_id %s conflicts with "
                "authoritative value %s (likely resolved to wrong title)",
                provider_name,
                ext.tmdb_id,
                anchor_tmdb_id,
            )
            continue
        validated.append((provider_name, ext))

    return validated


def _external_ids_to_dict(ext: ExternalIds) -> dict:
    """Convert ExternalIds to a dict for caching."""
    result: dict = {}
    if ext.imdb_id:
        result["imdb_id"] = ext.imdb_id
    if ext.tmdb_id:
        result["tmdb_id"] = ext.tmdb_id
    if ext.tmdb_kind:
        result["tmdb_kind"] = ext.tmdb_kind
    if ext.tvdb_id:
        result["tvdb_id"] = ext.tvdb_id
    if ext.anilist_id:
        result["anilist_id"] = ext.anilist_id
    return result


def _cached_to_result(cached: dict, provider_name: str, kind: str) -> Optional[MetadataResult]:
    """Convert a cached provider dict back to a MetadataResult."""
    if provider_name == "tmdb":
        detail = cached.get("detail", {})
        ext_raw = cached.get("external_ids", {})
        title = detail.get("title") or detail.get("name")
        date = detail.get("release_date") or detail.get("first_air_date")
        year = int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None
        tmdb_id = detail.get("id")
        return MetadataResult(
            title=title,
            year=year,
            kind=kind,
            external_ids=ExternalIds(
                imdb_id=ext_raw.get("imdb_id"),
                tmdb_id=tmdb_id,
                tmdb_kind=kind,
                tvdb_id=ext_raw.get("tvdb_id"),
            ),
            source="tmdb",
            raw=cached,
        )
    elif provider_name == "simkl":
        response = cached.get("response", cached)
        if response.get("type") == "episode" and "show" in response:
            info = response["show"]
        elif response.get("type") == "movie" and "movie" in response:
            info = response["movie"]
        else:
            return None
        ids = info.get("ids", {})
        tmdb_id = ids.get("tmdbtv") or ids.get("tmdb") or ids.get("moviedb")
        if tmdb_id:
            tmdb_id = int(tmdb_id)
        return MetadataResult(
            title=info.get("title"),
            year=info.get("year"),
            kind=kind,
            external_ids=ExternalIds(
                imdb_id=ids.get("imdb"),
                tmdb_id=tmdb_id,
                tmdb_kind=kind,
                tvdb_id=ids.get("tvdb"),
            ),
            source="simkl",
            raw=cached,
        )
    elif provider_name == "omdb":
        title = cached.get("Title")
        year_str = cached.get("Year")
        year = int(year_str[:4]) if year_str and len(year_str) >= 4 and year_str[:4].isdigit() else None
        enriched = cached.get("_enriched_ids", {})
        return MetadataResult(
            title=title,
            year=year,
            kind=kind,
            external_ids=ExternalIds(
                imdb_id=cached.get("imdbID"),
                tmdb_id=enriched.get("tmdb_id"),
                tmdb_kind=enriched.get("tmdb_kind"),
                tvdb_id=enriched.get("tvdb_id"),
            ),
            source="omdb",
            raw=cached,
        )
    elif provider_name == "tvdb":
        from unshackle.core.providers.tvdb import _ids_from_remote, _parse_int

        tvdb_id = _parse_int(cached.get("tvdb_id") or cached.get("id"))
        ext = _ids_from_remote(cached.get("remote_ids") or cached.get("remoteIds"), tvdb_id)
        # restore IDs that enrichment filled in beyond the raw remote_ids
        enriched = cached.get("_enriched_ids", {})
        ext.imdb_id = ext.imdb_id or enriched.get("imdb_id")
        ext.tmdb_id = ext.tmdb_id or enriched.get("tmdb_id")
        if ext.tmdb_id:
            ext.tmdb_kind = kind
        return MetadataResult(
            title=cached.get("name"),
            year=_parse_int(cached.get("year")),
            kind=kind,
            external_ids=ext,
            source="tvdb",
            raw=cached,
        )
    elif provider_name == "anilist":
        return AniListProvider()._to_result(cached)
    elif provider_name == "imdb":
        from unshackle.core.providers.imdb import primary_language

        title = (cached.get("titleText") or {}).get("text") or (cached.get("originalTitleText") or {}).get("text")
        year = (cached.get("releaseYear") or {}).get("year")
        imdb_id = cached.get("id")
        # Restore enriched IDs that were saved alongside the raw data
        enriched = cached.get("_enriched_ids", {})
        return MetadataResult(
            title=title,
            year=year,
            kind=kind,
            external_ids=ExternalIds(
                imdb_id=imdb_id,
                tmdb_id=enriched.get("tmdb_id"),
                tmdb_kind=enriched.get("tmdb_kind"),
                tvdb_id=enriched.get("tvdb_id"),
            ),
            original_language=primary_language(cached),
            source="imdb",
            raw=cached,
        )
    return None


__all__ = [
    "DEFAULT_ORDER",
    "REGISTRY",
    "ExternalIds",
    "MetadataProvider",
    "MetadataResult",
    "enrich_ids",
    "fetch_external_ids",
    "fuzzy_match",
    "get_available_providers",
    "get_provider",
    "get_title_by_id",
    "get_year_by_id",
    "provider_order",
    "resolve_by_ids",
    "search_metadata",
]
