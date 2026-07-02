from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from unshackle.core.titles.music import Song


@dataclass
class MusicTrackOption:
    codec: str
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    bitrate: Optional[int] = None
    channels: Optional[float] = None
    lossless: bool = False
    hires: bool = False
    atmos: bool = False
    explicit: bool = False
    duration: Optional[int] = None
    quality_label: str = ""


@dataclass
class MusicSongPlan:
    song: Song
    options: list[MusicTrackOption] = field(default_factory=list)
    selected: Optional[MusicTrackOption] = None
    output_path: Optional[Path] = None
    fallback_used: bool = False
    skip_reason: str = ""


@dataclass
class MusicDiscPlan:
    disc_number: int
    songs: list[MusicSongPlan] = field(default_factory=list)


@dataclass
class MusicDownloadPlan:
    kind: str
    title: str
    artist: str
    album_artist: str = ""
    year: Optional[int] = None
    released: str = ""
    genre: str = ""
    label: str = ""
    artwork_url: Optional[str] = None
    total_tracks: Optional[int] = None
    total_discs: Optional[int] = None
    total_duration: Optional[int] = None
    discs: list[MusicDiscPlan] = field(default_factory=list)
    quality_requested: str = "best"
    fallback_mode: str = "next-best"
