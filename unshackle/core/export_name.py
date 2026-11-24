"""The file name of a ``dl --export`` file, built from the titles and tracks it holds.

The name has the form ``{title}.{scope}.{resolutions}.{codecs}.{ranges}.{manifests}.{audio}-{TAG}``,
for example ``Example.Show.S01.2160p.h265.sdr.hdr10.dash.ddp5.1-EXAMPLE``. An empty part is left out.
The name always uses dots and never reads the output templates.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from unshackle.core.constants import AnyTrack
from unshackle.core.titles import Episode, Movie, Song, Title_T
from unshackle.core.tracks import Audio, Video
from unshackle.core.utilities import sanitize_filename

STANDARD_HEIGHTS = (4320, 2160, 1440, 1080, 720, 576, 480, 360)
MANIFEST_ORDER = (Video.Descriptor.DASH, Video.Descriptor.HLS, Video.Descriptor.ISM, Video.Descriptor.URL)
MAX_SCOPE_PARTS = 3
MAX_TITLE_LENGTH = 100


def season_episodes(titles: Iterable[Any]) -> dict[int, set[int]]:
    """The episode numbers of each season in ``titles``, to tell a whole season from part of one."""
    seasons: dict[int, set[int]] = {}
    for t in titles:
        if isinstance(t, Episode) and t.number is not None:
            seasons.setdefault(t.season or 0, set()).add(t.number)
    return seasons


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def title_label(title: Any) -> str:
    """A short label for one title in a message, e.g. ``S01E05 'Name'`` or ``'Name' (2024)``."""
    if isinstance(title, Episode):
        label = f"S{title.season or 0:02}E{title.number or 0:02}"
        return f"{label} '{title.name}'" if title.name else label
    if isinstance(title, Song):
        return f"'{title.name}' by {title.artist}"
    if isinstance(title, Movie):
        return f"'{title.name}' ({title.year})" if title.year else f"'{title.name}'"
    return f"'{title}'"


def title_part(titles: Sequence[Title_T]) -> str:
    first = titles[0]
    if isinstance(first, Episode):
        text = first.title
    elif isinstance(first, Song):
        text = f"{first.artist} - {first.album or first.name}"
    elif isinstance(first, Movie):
        text = f"{first.name} {first.year}" if first.year else first.name
        others = len({t.id for t in titles}) - 1
        if others:
            text += f" +{others}"
    else:
        text = str(first)
    return sanitize_filename(text, spacer=".", unicode=True)[:MAX_TITLE_LENGTH].strip(" .")


def scope_part(titles: Sequence[Title_T], seasons: Mapping[int, Collection[int]]) -> str:
    """``S01`` for a whole season, ``S01E05-E09`` for a run of episodes, runs joined with ``.``.

    A season counts as whole only when every episode the service listed for it is in ``titles``.
    Whole seasons in a row collapse to ``S01-S03``. After ``MAX_SCOPE_PARTS`` parts, ``+N``
    counts the parts left out.
    """
    exported = season_episodes(titles)
    parts: list[str] = []
    whole_run: list[int] = []

    def close_whole_run() -> None:
        if whole_run:
            first, last = whole_run[0], whole_run[-1]
            parts.append(f"S{first:02}" if first == last else f"S{first:02}-S{last:02}")
            whole_run.clear()

    for season in sorted(exported):
        numbers = sorted(exported[season])
        listed = seasons.get(season)
        if listed and set(listed) <= exported[season]:
            if whole_run and whole_run[-1] != season - 1:
                close_whole_run()
            whole_run.append(season)
            continue
        close_whole_run()
        start = prev = numbers[0]
        for n in [*numbers[1:], None]:
            if n is not None and n == prev + 1:
                prev = n
                continue
            run = f"S{season:02}E{start:02}"
            parts.append(run if start == prev else f"{run}-E{prev:02}")
            if n is not None:
                start = prev = n
    close_whole_run()

    if len(parts) > MAX_SCOPE_PARTS:
        parts = [*parts[:MAX_SCOPE_PARTS], f"+{len(parts) - MAX_SCOPE_PARTS}"]
    return ".".join(parts)


def resolution(track: Video) -> Optional[int]:
    """The resolution a release name uses: a letterboxed 1920x800 is 1080p, from its width."""
    if not track.height:
        return None
    height = track.height
    if track.width and track.width > height:
        height = max(height, track.width * 9 // 16)
    return next((std for std in STANDARD_HEIGHTS if abs(height - std) <= 16), height)


def audio_token(track: Audio) -> str:
    """``dd5.1``, ``ddp5.1atmos``, ``aac2.0``: the codec and channels of one audio track."""
    codec = track.codec.value.lower().replace("+", "p").replace(" ", "") if track.codec else "audio"
    channels = track.channels
    if isinstance(channels, float):
        channels = f"{channels:.1f}"
    return f"{codec}{channels or ''}{'atmos' if track.joc else ''}"


def quality_parts(tracks: Iterable[AnyTrack]) -> list[str]:
    tracks = list(tracks)
    videos = [t for t in tracks if isinstance(t, Video)]
    audios = [t for t in tracks if isinstance(t, Audio)]
    codec_order = list(Video.Codec)
    range_order = list(Video.Range)
    audio_order = list(Audio.Codec)

    heights = sorted({h for v in videos if (h := resolution(v))}, reverse=True)
    codecs = sorted({v.codec for v in videos if v.codec}, key=codec_order.index)
    ranges = sorted({v.range for v in videos if v.range}, key=range_order.index)
    media = videos + audios or tracks
    manifests = [d for d in MANIFEST_ORDER if any(t.descriptor == d for t in media)]
    audio_sorted = sorted(
        audios,
        key=lambda a: (
            audio_order.index(a.codec) if a.codec else len(audio_order),
            -Audio.channel_total(a.channels) if a.channels else 0,
            not a.joc,
        ),
    )

    groups = [
        [f"{h}p" for h in heights],
        [c.value.lower().replace(".", "") for c in codecs],
        [r.value.lower() for r in ranges],
        [d.name.lower() for d in manifests],
        list(dict.fromkeys(audio_token(a) for a in audio_sorted)),
    ]
    return [token for group in groups for token in group]


def export_name(
    titles: Sequence[Title_T],
    tracks: Iterable[AnyTrack],
    service_tag: str,
    seasons: Optional[Mapping[int, Collection[int]]] = None,
) -> str:
    """The export file name without ``.json``, from the exported ``titles`` and ``tracks``.

    ``seasons`` holds the episode numbers of each season the service listed. Without it,
    a season is never whole and its episodes show as ranges.
    """
    head = title_part(titles) if titles else ""
    scope = scope_part(titles, seasons or {}) if titles and isinstance(titles[0], Episode) else ""
    return ".".join(p for p in (head, scope, *quality_parts(tracks)) if p) + f"-{service_tag}"


def move_export(current: Path, name: str) -> Path:
    """Move the run's export file to ``name`` in its folder, and return the new path.

    It never writes over another file at that name: the name gets ``-2``, ``-3``, and so on.
    """
    target = current.with_name(f"{name}.json")
    n = 2
    while target != current and target.exists():
        target = current.with_name(f"{name}-{n}.json")
        n += 1
    if target != current:
        os.replace(current, target)
    return target


class ExportNamer:
    """The state of one run's export: its titles, tracks, and the current file path."""

    def __init__(self, service_tag: str, seasons: Optional[Mapping[int, Collection[int]]] = None) -> None:
        self.service_tag = service_tag
        self.seasons: dict[int, set[int]] = {season: set(numbers) for season, numbers in (seasons or {}).items()}
        self.titles: dict[str, Title_T] = {}
        self.tracks: dict[str, AnyTrack] = {}
        self.path: Optional[Path] = None

    def add(self, title: Title_T, track: AnyTrack) -> None:
        """Record ``track`` and the video and audio tracks selected for ``title``.

        The first write of a title is often a subtitle, and a name from that track alone would
        leave out the quality of the title.
        """
        self.titles.setdefault(str(title.id), title)
        for t in [track, *title.tracks.videos, *title.tracks.audio]:
            self.tracks.setdefault(f"{title.id}/{t.id}", t)

    def name(self) -> str:
        return export_name(list(self.titles.values()), self.tracks.values(), self.service_tag, self.seasons)
