from abc import ABC
from typing import Any, Iterable, Optional, Union

from langcodes import Language
from pymediainfo import MediaInfo
from rich.tree import Tree
from sortedcontainers import SortedKeyList

from unshackle.core.config import config
from unshackle.core.constants import AUDIO_CODEC_MAP
from unshackle.core.titles.title import Title
from unshackle.core.utilities import sanitize_filename
from unshackle.core.utils.template_formatter import TemplateFormatter


class Song(Title):
    def __init__(
        self,
        id_: Any,
        service: type,
        name: str,
        artist: str,
        album: str,
        track: int,
        disc: int,
        year: int,
        language: Optional[Union[str, Language]] = None,
        data: Optional[Any] = None,
    ) -> None:
        super().__init__(id_, service, language, data)

        if not name:
            raise ValueError("Song name must be provided")
        if not isinstance(name, str):
            raise TypeError(f"Expected name to be a str, not {name!r}")

        if not artist:
            raise ValueError("Song artist must be provided")
        if not isinstance(artist, str):
            raise TypeError(f"Expected artist to be a str, not {artist!r}")

        if not album:
            raise ValueError("Song album must be provided")
        if not isinstance(album, str):
            raise TypeError(f"Expected album to be a str, not {name!r}")

        if not track:
            raise ValueError("Song track must be provided")
        if not isinstance(track, int):
            raise TypeError(f"Expected track to be an int, not {track!r}")

        if not disc:
            raise ValueError("Song disc must be provided")
        if not isinstance(disc, int):
            raise TypeError(f"Expected disc to be an int, not {disc!r}")

        if not year:
            raise ValueError("Song year must be provided")
        if not isinstance(year, int):
            raise TypeError(f"Expected year to be an int, not {year!r}")

        name = name.strip()
        artist = artist.strip()
        album = album.strip()

        if track <= 0:
            raise ValueError(f"Song track cannot be {track}")
        if disc <= 0:
            raise ValueError(f"Song disc cannot be {disc}")
        if year <= 0:
            raise ValueError(f"Song year cannot be {year}")

        self.name = name
        self.artist = artist
        self.album = album
        self.track = track
        self.disc = disc
        self.year = year

    def __str__(self) -> str:
        return "{artist} - {album} ({year}) / {track:02}. {name}".format(
            artist=self.artist, album=self.album, year=self.year, track=self.track, name=self.name
        ).strip()

    def _build_template_context(self, media_info: MediaInfo, show_service: bool = True) -> dict:
        """Build template context dictionary from MediaInfo."""
        primary_audio_track = next(iter(media_info.audio_tracks), None)

        context = {
            "artist": self.artist.replace("$", "S"),
            "album": self.album.replace("$", "S"),
            "title": self.name.replace("$", "S"),
            "track_number": f"{self.track:02}",
            "disc": f"{self.disc:02}" if self.disc > 1 else "",
            "year": self.year or "",
            "tag": config.tag or "",
            "source": self.service.__name__ if show_service else "",
        }

        # Audio information
        if primary_audio_track:
            codec = primary_audio_track.format
            channel_layout = primary_audio_track.channel_layout or primary_audio_track.channellayout_original

            if channel_layout:
                channels = float(sum({"LFE": 0.1}.get(position.upper(), 1) for position in channel_layout.split(" ")))
            else:
                channel_count = primary_audio_track.channel_s or primary_audio_track.channels or 0
                channels = float(channel_count)

            features = primary_audio_track.format_additionalfeatures or ""

            context.update(
                {
                    "audio": AUDIO_CODEC_MAP.get(codec, codec),
                    "audio_channels": f"{channels:.1f}",
                    "audio_full": f"{AUDIO_CODEC_MAP.get(codec, codec)}{channels:.1f}",
                    "atmos": "Atmos" if ("JOC" in features or primary_audio_track.joc) else "",
                }
            )

        return context

    def get_filename(self, media_info: MediaInfo, folder: bool = False, show_service: bool = True) -> str:
        if folder:
            # For folders, use simple naming: "Artist - Album (Year)"
            name = f"{self.artist} - {self.album}"
            if self.year:
                name += f" ({self.year})"
            return sanitize_filename(name, " ")

        # Use template from output_template (which includes scene_naming compatibility)
        # or fallback to default scene-style template
        template = (
            config.output_template.get("songs") or "{track_number}.{title}.{source?}.WEB-DL.{audio_full}.{atmos?}-{tag}"
        )

        formatter = TemplateFormatter(template)
        context = self._build_template_context(media_info, show_service)
        return formatter.format(context)


class Album(SortedKeyList, ABC):
    def __init__(self, iterable: Optional[Iterable] = None):
        super().__init__(iterable, key=lambda x: (x.album, x.disc, x.track, x.year or 0))

    def __str__(self) -> str:
        if not self:
            return super().__str__()
        return f"{self[0].artist} - {self[0].album} ({self[0].year or '?'})"

    def tree(self, verbose: bool = False) -> Tree:
        num_songs = len(self)
        tree = Tree(f"{num_songs} Song{['s', ''][num_songs == 1]}", guide_style="bright_black")
        if verbose:
            for song in self:
                tree.add(f"[bold]Track {song.track:02}.[/] [bright_black]({song.name})", guide_style="bright_black")

        return tree


__all__ = ("Song", "Album")
