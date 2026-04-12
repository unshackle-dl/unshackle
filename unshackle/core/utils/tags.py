from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.providers import (ExternalIds, MetadataResult, enrich_ids, fetch_external_ids, fuzzy_match,
                                      get_available_providers, get_provider, search_metadata)
from unshackle.core.titles.episode import Episode
from unshackle.core.titles.movie import Movie
from unshackle.core.titles.title import Title

log = logging.getLogger("TAGS")


def apply_tags(path: Path, tags: dict[str, str]) -> None:
    if not tags:
        return
    if not binaries.Mkvpropedit:
        log.debug("mkvpropedit not found on PATH; skipping tags")
        return
    log.debug("Applying tags to %s: %s", path, tags)
    xml_lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<Tags>", "  <Tag>", "    <Targets/>"]
    for name, value in tags.items():
        xml_lines.append(f"    <Simple><Name>{escape(name)}</Name><String>{escape(value)}</String></Simple>")
    xml_lines.extend(["  </Tag>", "</Tags>"])
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write("\n".join(xml_lines))
        tmp_path = Path(f.name)
    try:
        result = subprocess.run(
            [str(binaries.Mkvpropedit), str(path), "--tags", f"global:{tmp_path}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning("mkvpropedit failed (exit %d): %s", result.returncode, result.stderr.strip())
        else:
            log.debug("Tags applied via mkvpropedit")
    finally:
        tmp_path.unlink(missing_ok=True)


def _build_tags_from_ids(ids: ExternalIds, kind: str) -> dict[str, str]:
    """Build standard MKV tags from external IDs."""
    tags: dict[str, str] = {}
    if ids.imdb_id:
        tags["IMDB"] = ids.imdb_id
    if ids.tmdb_id and ids.tmdb_kind:
        tags["TMDB"] = f"{ids.tmdb_kind}/{ids.tmdb_id}"
    if ids.tvdb_id:
        prefix = "movies" if kind == "movie" else "series"
        tags["TVDB2"] = f"{prefix}/{ids.tvdb_id}"
    return tags


def tag_file(
    path: Path,
    title: Title,
    tmdb_id: Optional[int] = None,
    imdb_id: Optional[str] = None,
) -> None:
    log.debug("Tagging file %s with title %r", path, title)
    custom_tags: dict[str, str] = {}

    if config.tag and config.tag_group_name:
        custom_tags["Group"] = config.tag
    description = getattr(title, "description", None)
    if description:
        if len(description) > 255:
            truncated = description[:255]
            if " " in truncated:
                truncated = truncated.rsplit(" ", 1)[0]
            description = truncated + "..."
        custom_tags["Description"] = description

    if isinstance(title, Movie):
        kind = "movie"
        name = title.name
        year = title.year
    elif isinstance(title, Episode):
        kind = "tv"
        name = title.title
        year = title.year
    else:
        apply_tags(path, custom_tags)
        return

    standard_tags: dict[str, str] = {}

    if config.tag_imdb_tmdb:
        try:
            providers = get_available_providers()
            if not providers:
                log.debug("No metadata providers available; skipping tag lookup")
                apply_tags(path, custom_tags)
                return

            result: Optional[MetadataResult] = None

            # Direct ID lookup path
            if imdb_id:
                imdbapi = get_provider("imdbapi")
                if imdbapi:
                    result = imdbapi.get_by_id(imdb_id, kind)
                    if result:
                        result.external_ids.imdb_id = imdb_id
                        enrich_ids(result)
            elif tmdb_id is not None:
                tmdb = get_provider("tmdb")
                if tmdb:
                    result = tmdb.get_by_id(tmdb_id, kind)
                    if result:
                        ext = tmdb.get_external_ids(tmdb_id, kind)
                        result.external_ids = ext
            else:
                # Search across providers in priority order
                result = search_metadata(name, year, kind)

            # If we got a TMDB ID from search but no full external IDs, fetch them
            if result and result.external_ids.tmdb_id and not result.external_ids.imdb_id:
                ext = fetch_external_ids(result.external_ids.tmdb_id, kind)
                if ext.imdb_id:
                    result.external_ids.imdb_id = ext.imdb_id
                if ext.tvdb_id:
                    result.external_ids.tvdb_id = ext.tvdb_id

            if result and result.external_ids:
                standard_tags = _build_tags_from_ids(result.external_ids, kind)
        except Exception as e:
            log.warning("Metadata lookup failed, applying custom tags only: %s", e)

    apply_tags(path, {**custom_tags, **standard_tags})


__all__ = [
    "apply_tags",
    "fuzzy_match",
    "tag_file",
]
