from __future__ import annotations

import logging
from typing import Optional

from unshackle.core.providers._base import ExternalIds

log = logging.getLogger("ANIMEAPI")

PLATFORM_MAP: dict[str, str] = {
    "mal": "myanimelist",
    "anilist": "anilist",
    "kitsu": "kitsu",
    "tmdb": "themoviedb",
    "trakt": "trakt",
    "tvdb": "thetvdb",
}


def resolve_animeapi(value: str) -> tuple[Optional[str], ExternalIds]:
    """Resolve an anime database ID via AnimeAPI to a title and external IDs.

    Accepts formats like 'mal:12345', 'anilist:98765', or just '12345' (defaults to MAL).
    Returns (anime_title, ExternalIds) with any TMDB/IMDB/TVDB IDs found.
    """
    import animeapi

    platform_str, id_str = _parse_animeapi_value(value)

    platform_enum = _get_platform(platform_str)
    if platform_enum is None:
        log.warning("Unknown AnimeAPI platform: %s (supported: %s)", platform_str, ", ".join(PLATFORM_MAP))
        return None, ExternalIds()

    log.info("Resolving AnimeAPI %s:%s", platform_str, id_str)

    try:
        with animeapi.AnimeAPI() as api:
            relation = api.get_anime_relations(id_str, platform_enum)
    except Exception as exc:
        log.warning("AnimeAPI lookup failed for %s:%s: %s", platform_str, id_str, exc)
        return None, ExternalIds()

    title = getattr(relation, "title", None)

    tmdb_id = getattr(relation, "themoviedb", None)
    tmdb_type = getattr(relation, "themoviedb_type", None)
    imdb_id = getattr(relation, "imdb", None)
    tvdb_id = getattr(relation, "thetvdb", None)

    tmdb_kind: Optional[str] = None
    if tmdb_type is not None:
        tmdb_kind = tmdb_type.value if hasattr(tmdb_type, "value") else str(tmdb_type).lower()
        if tmdb_kind not in ("movie", "tv"):
            tmdb_kind = "tv"

    external_ids = ExternalIds(
        tmdb_id=int(tmdb_id) if tmdb_id is not None else None,
        tmdb_kind=tmdb_kind,
        imdb_id=str(imdb_id) if imdb_id is not None else None,
        tvdb_id=int(tvdb_id) if tvdb_id is not None else None,
    )

    log.info(
        "AnimeAPI resolved: title=%r, tmdb=%s, imdb=%s, tvdb=%s",
        title,
        external_ids.tmdb_id,
        external_ids.imdb_id,
        external_ids.tvdb_id,
    )

    return title, external_ids


def _parse_animeapi_value(value: str) -> tuple[str, str]:
    """Parse 'platform:id' format. Defaults to 'mal' if no prefix."""
    if ":" in value:
        platform, _, id_str = value.partition(":")
        return platform.lower().strip(), id_str.strip()
    return "mal", value.strip()


def _get_platform(platform_str: str) -> object | None:
    """Map a platform string to an animeapi.Platform enum value."""
    import animeapi

    canonical = PLATFORM_MAP.get(platform_str)
    if canonical is None:
        return None

    platform_name = canonical.upper()
    return getattr(animeapi.Platform, platform_name, None)
