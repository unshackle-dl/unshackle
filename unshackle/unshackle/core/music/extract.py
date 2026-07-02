"""Shared, stateless helpers for music services.

These functions consolidate the generic data-shaping logic that music services
otherwise each duplicate: first-non-empty getters, duration/year/name
formatting, release-kind classification, track-option dedupe, and Song -> Music
assembly. They take plain data in and return plain data out (no ``self``), so
any music service can reuse them.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from unshackle.core.music.models import MusicTrackOption
from unshackle.core.titles.music import Music, Song


def first_text(*values: Any, default: str = "") -> str:
    """Return the first non-empty stripped string across ``values``.

    Dicts are searched by common label keys; lists are joined with ", ".
    Falls back to ``default`` when nothing usable is found.
    """
    for value in values:
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            for key in ("name", "title", "display", "display_name", "description", "url"):
                nested = value.get(key)
                text = first_text(nested) if isinstance(nested, (dict, list)) else str(nested or "").strip()
                if text:
                    return text
        elif isinstance(value, list):
            parts = [first_text(item) for item in value]
            text = ", ".join(part for part in parts if part)
            if text:
                return text
        else:
            text = str(value).strip()
            if text:
                return text
    return default


def first_number(*values: Any) -> Optional[float]:
    """Return the first value parseable as a float, else ``None``."""
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def year_from_value(value: Any) -> int:
    """Extract a 4-digit year from ``value``; fall back to 1900 when absent."""
    match = re.search(r"(?P<year>\d{4})", str(value or ""))
    return int(match.group("year")) if match else 1900


def duration_seconds(value: Any) -> Optional[float]:
    """Coerce a duration to seconds, treating large numbers (>10000) as milliseconds."""
    number = first_number(value)
    if number is None:
        return None
    if number > 10_000:
        return number / 1000
    return number


def format_duration(value: Any) -> str:
    """Format a duration in seconds as ``H:MM:SS`` (or ``M:SS`` under an hour)."""
    seconds_value = first_number(value)
    if seconds_value is None:
        return ""
    total = max(0, int(round(seconds_value)))
    minutes, remaining = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02}:{remaining:02}"
    return f"{minutes}:{remaining:02}"


def format_names(value: Any, sep: str = ", ") -> str:
    """Join a list/dict of artist-like entries into a de-duplicated string."""
    names: list[str] = []
    values: Any = value
    if isinstance(values, dict):
        values = values.get("items") or values.get("artists") or [values]
    if not isinstance(values, list):
        values = [values]
    for item in values:
        name = first_text(
            item.get("profile") if isinstance(item, dict) else None,
            item.get("artist") if isinstance(item, dict) else None,
            item if isinstance(item, dict) else None,
            item,
            default="",
        )
        if name and name not in names:
            names.append(name)
    return sep.join(names)


def classify_release_kind(raw_kind: str, tracks_count: Optional[float]) -> str:
    """Normalise an already-extracted release-kind string to a canonical value.

    Returns one of ``single | ep | album | compilation | live | download |
    playlist | other``. ``tracks_count`` disambiguates "single" (1 track) from
    an EP (more than 1 track) for sources that label EPs as singles; pass the
    real track count (or ``None`` only when genuinely unknown) — it is required
    so no caller silently mislabels a multi-track "single" as an EP by omission.
    """
    key = re.sub(r"[^a-z0-9]+", "", str(raw_kind or "").lower())

    if key in {"single"}:
        return "single" if tracks_count == 1 else "ep"
    if key in {"ep", "extendedplay"}:
        return "ep"
    if key in {"epsingle", "epsingles"}:
        return "single" if tracks_count == 1 else "ep"
    if key in {"compilation", "compilations"}:
        return "compilation"
    if key in {"live", "liverecording"}:
        return "live"
    if key in {"download", "downloads"}:
        return "download"
    if key in {"playlist", "playlists"}:
        return "playlist"
    if key in {"other"}:
        return "other"
    return "album"


def dedupe_track_options(options: list[MusicTrackOption]) -> list[MusicTrackOption]:
    """Drop duplicate track options keyed on codec/quality identity, preserving order."""
    seen: set[tuple[str, Optional[int], Optional[int], Optional[int], str, bool]] = set()
    unique: list[MusicTrackOption] = []
    for option in options:
        key = (
            str(option.codec or "").upper(),
            option.bit_depth,
            option.sample_rate,
            option.bitrate,
            option.quality_label,
            option.explicit,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(option)
    return unique


def build_music_from_songs(
    songs: list[Song],
    *,
    kind: str,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    owner: Optional[str] = None,
    description: Optional[str] = None,
    empty_error: str = "No songs were returned.",
) -> Music:
    """Assemble a :class:`Music` release from a list of :class:`Song` objects.

    Aggregates artwork and total duration from each song's ``data`` payload and
    derives track/disc totals. Raises ``ValueError(empty_error)`` on an empty list.
    """
    if not songs:
        raise ValueError(empty_error)

    first_song = songs[0]
    artwork_url = ""
    total_duration = 0
    for song in songs:
        data = song.data if isinstance(song.data, dict) else {}
        artwork_url = artwork_url or first_text(song.artwork_url, data.get("artwork_url"))
        total_duration += int(first_number(data.get("duration")) or 0)

    return Music(
        songs,
        kind=kind,
        title=title or first_song.album,
        artist=artist or first_song.album_artist or first_song.artist,
        year=first_song.year,
        total_tracks=max((song.total_tracks or song.track for song in songs), default=len(songs)),
        total_discs=max((song.total_discs or song.disc for song in songs), default=1),
        artwork_url=artwork_url or None,
        total_duration=total_duration or None,
        owner=owner,
        description=description,
    )
