from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Union
from xml.sax.saxutils import escape

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.music.tagger import write_music_metadata
from unshackle.core.providers import (
    ExternalIds,
    fuzzy_match,
    get_available_providers,
    resolve_by_ids,
)
from unshackle.core.titles.episode import Episode
from unshackle.core.titles.movie import Movie
from unshackle.core.titles.music import Song
from unshackle.core.titles.title import Title
from unshackle.core.utils.subprocess import log_tool_run

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
        tag_start = time.monotonic()
        result = subprocess.run(
            [str(binaries.Mkvpropedit), str(path), "--tags", f"global:{tmp_path}"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        log_tool_run(
            "mkvpropedit tags",
            "mkvpropedit",
            result.returncode,
            duration_ms=round((time.monotonic() - tag_start) * 1000, 1),
            file=Path(path).name,
            tag_count=len(tags),
        )
        if result.returncode != 0:
            log.warning("mkvpropedit failed (exit %d): %s", result.returncode, result.stderr.strip())
        else:
            log.debug("Tags applied via mkvpropedit")
    finally:
        tmp_path.unlink(missing_ok=True)


def build_tags_from_ids(ids: ExternalIds, kind: str) -> dict[str, str]:
    """Build standard MKV tags from external IDs."""
    tags: dict[str, str] = {}
    if ids.imdb_id:
        tags["IMDB"] = ids.imdb_id
    if ids.tmdb_id and ids.tmdb_kind:
        tags["TMDB"] = f"{ids.tmdb_kind}/{ids.tmdb_id}"
    if ids.tvdb_id:
        prefix = "movies" if kind == "movie" else "series"
        tags["TVDB2"] = f"{prefix}/{ids.tvdb_id}"
    if ids.anilist_id:
        tags["ANILIST"] = str(ids.anilist_id)
    return tags


def tag_file(
    path: Path,
    title: Title,
    tmdb_id: Optional[int] = None,
    imdb_id: Optional[str] = None,
    tvdb_id: Optional[int] = None,
    anilist_id: Optional[Union[int, str]] = None,
    anime: bool = False,
    session: Any = None,
) -> None:
    log.debug("Tagging file %s with title %r", path, title)

    if isinstance(title, Song):
        try:
            music_result = write_music_metadata(path, title, session=session)
        except Exception as e:  # the file already moved; a tagging fault must warn, not abort the run
            log.warning("Music tagging failed for %s: %s", path.name, e)
            return
        if music_result.skipped and music_result.reason:
            log.warning("Music metadata skipped for %s: %s", path.name, music_result.reason)
        return

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
            has_ids = any(value is not None for value in (tmdb_id, imdb_id, tvdb_id, anilist_id))
            if not get_available_providers() and not has_ids:
                log.debug("No metadata providers available; skipping tag lookup")
                apply_tags(path, custom_tags)
                return

            result = resolve_by_ids(
                tmdb_id, imdb_id, tvdb_id, anilist_id, title=name, year=year, kind=kind, anime=anime
            )

            if result and result.external_ids:
                standard_tags = build_tags_from_ids(result.external_ids, kind)
        except Exception as e:
            log.warning("Metadata lookup failed, applying custom tags only: %s", e)

    apply_tags(path, {**custom_tags, **standard_tags})


__all__ = [
    "apply_tags",
    "fuzzy_match",
    "tag_file",
]
