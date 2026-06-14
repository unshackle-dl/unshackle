import re
from abc import ABC
from typing import Any, Iterable, Optional, Union

from langcodes import Language
from pymediainfo import MediaInfo
from sortedcontainers import SortedKeyList

from unshackle.core.config import config
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
        album_artist: Optional[str] = None,
        release_type: str = "album",
        total_tracks: Optional[int] = None,
        total_discs: Optional[int] = None,
        genre: Optional[str] = None,
        explicit: Optional[bool] = None,
        isrc: Optional[str] = None,
        upc: Optional[str] = None,
        copyright: Optional[str] = None,
        label: Optional[str] = None,
        lyrics: Optional[str] = None,
        artwork_url: Optional[str] = None,
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
            raise TypeError(f"Expected album to be a str, not {album!r}")

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
        if album_artist is not None and not isinstance(album_artist, str):
            raise TypeError(f"Expected album_artist to be a str, not {album_artist!r}")
        if not isinstance(release_type, str):
            raise TypeError(f"Expected release_type to be a str, not {release_type!r}")
        if total_tracks is not None and not isinstance(total_tracks, int):
            raise TypeError(f"Expected total_tracks to be an int, not {total_tracks!r}")
        if total_discs is not None and not isinstance(total_discs, int):
            raise TypeError(f"Expected total_discs to be an int, not {total_discs!r}")
        if genre is not None and not isinstance(genre, str):
            raise TypeError(f"Expected genre to be a str, not {genre!r}")
        if explicit is not None and not isinstance(explicit, bool):
            raise TypeError(f"Expected explicit to be a bool, not {explicit!r}")
        if isrc is not None and not isinstance(isrc, str):
            raise TypeError(f"Expected isrc to be a str, not {isrc!r}")
        if upc is not None and not isinstance(upc, str):
            raise TypeError(f"Expected upc to be a str, not {upc!r}")
        if copyright is not None and not isinstance(copyright, str):
            raise TypeError(f"Expected copyright to be a str, not {copyright!r}")
        if label is not None and not isinstance(label, str):
            raise TypeError(f"Expected label to be a str, not {label!r}")
        if lyrics is not None and not isinstance(lyrics, str):
            raise TypeError(f"Expected lyrics to be a str, not {lyrics!r}")
        if artwork_url is not None and not isinstance(artwork_url, str):
            raise TypeError(f"Expected artwork_url to be a str, not {artwork_url!r}")

        name = name.strip()
        artist = artist.strip()
        album = album.strip()
        album_artist = album_artist.strip() if album_artist else None
        release_type = release_type.strip().lower()
        genre = genre.strip() if genre else None
        isrc = isrc.strip() if isrc else None
        upc = upc.strip() if upc else None
        copyright = copyright.strip() if copyright else None
        label = label.strip() if label else None
        lyrics = lyrics.strip() if lyrics else None
        artwork_url = artwork_url.strip() if artwork_url else None

        if track <= 0:
            raise ValueError(f"Song track cannot be {track}")
        if disc <= 0:
            raise ValueError(f"Song disc cannot be {disc}")
        if year <= 0:
            raise ValueError(f"Song year cannot be {year}")
        if not release_type:
            raise ValueError("Song release_type must be provided")
        if total_tracks is not None and total_tracks <= 0:
            raise ValueError(f"Song total_tracks cannot be {total_tracks}")
        if total_discs is not None and total_discs <= 0:
            raise ValueError(f"Song total_discs cannot be {total_discs}")

        self.name = name
        self.artist = artist
        self.album = album
        self.track = track
        self.disc = disc
        self.year = year
        self.album_artist = album_artist
        self.release_type = release_type
        self.total_tracks = total_tracks
        self.total_discs = total_discs
        self.genre = genre
        self.explicit = explicit
        self.isrc = isrc
        self.upc = upc
        self.copyright = copyright
        self.label = label
        self.lyrics = lyrics
        self.artwork_url = artwork_url

    def __str__(self) -> str:
        return "{artist} - {album} ({year}) / {track:02}. {name}".format(
            artist=self.artist, album=self.album, year=self.year, track=self.track, name=self.name
        ).strip()

    def _build_template_context(self, media_info: MediaInfo, show_service: bool = True) -> dict:
        """Build template context dictionary from MediaInfo."""
        context = self._build_base_template_context(media_info, show_service)
        context["title"] = self.name.replace("$", "S")
        context["year"] = self.year or ""
        context["track_number"] = f"{self.track:02}"
        context["artist"] = self.artist.replace("$", "S")
        context["album_artist"] = (self.album_artist or self.artist).replace("$", "S")
        context["album"] = self.album.replace("$", "S")
        context["disc"] = f"{self.disc:02}" if self.disc > 1 else ""
        context["track_total"] = f"{self.total_tracks:02}" if self.total_tracks else ""
        context["disc_total"] = f"{self.total_discs:02}" if self.total_discs else ""
        context["release_type"] = self.release_type
        context["genre"] = self.genre or ""
        context["explicit"] = "Explicit" if self.explicit else ""
        context["isrc"] = self.isrc or ""
        context["upc"] = self.upc or ""
        context["label"] = self.label or ""
        return context

    def get_filename(self, media_info: MediaInfo, folder: bool = False, show_service: bool = True) -> str:
        if folder:
            # Album folder name: prefer the dedicated "albums" template, fall back to the
            # legacy "songs" folder template, then to "{artist} - {album} ({year})".
            template = config.get_folder_template("albums") or config.get_folder_template("songs")
            if template:
                formatter = TemplateFormatter(template)
                context = self._build_template_context(media_info, show_service)
                folder_name = formatter.format(context)

                separators = re.sub(r"\{[^}]*\}", "", template)
                spacer = "." if "." in separators and " " not in separators else " "
                return sanitize_filename(folder_name, spacer)
            name = f"{self.artist} - {self.album}"
            if self.year:
                name += f" ({self.year})"
            return sanitize_filename(name, " ")

        template = config.output_template.get("songs") or "{track_number}. {title}"
        formatter = TemplateFormatter(template)
        context = self._build_template_context(media_info, show_service)
        return formatter.format(context)


class Music(SortedKeyList, ABC):
    """A grouped music release, such as an album, EP, single, compilation, or playlist."""

    def __new__(cls, *args: Any, **kwargs: Any):
        return super().__new__(cls)

    def __init__(
        self,
        iterable: Optional[Iterable] = None,
        kind: str = "album",
        title: Optional[str] = None,
        artist: Optional[str] = None,
        year: Optional[int] = None,
        total_tracks: Optional[int] = None,
        total_discs: Optional[int] = None,
        artwork_url: Optional[str] = None,
        total_duration: Optional[int] = None,
        owner: Optional[str] = None,
        description: Optional[str] = None,
    ):
        if not isinstance(kind, str):
            raise TypeError(f"Expected kind to be a str, not {kind!r}")
        if title is not None and not isinstance(title, str):
            raise TypeError(f"Expected title to be a str, not {title!r}")
        if artist is not None and not isinstance(artist, str):
            raise TypeError(f"Expected artist to be a str, not {artist!r}")
        if year is not None and not isinstance(year, int):
            raise TypeError(f"Expected year to be an int, not {year!r}")
        if total_tracks is not None and not isinstance(total_tracks, int):
            raise TypeError(f"Expected total_tracks to be an int, not {total_tracks!r}")
        if total_discs is not None and not isinstance(total_discs, int):
            raise TypeError(f"Expected total_discs to be an int, not {total_discs!r}")
        if artwork_url is not None and not isinstance(artwork_url, str):
            raise TypeError(f"Expected artwork_url to be a str, not {artwork_url!r}")
        if total_duration is not None and not isinstance(total_duration, int):
            raise TypeError(f"Expected total_duration to be an int, not {total_duration!r}")
        if owner is not None and not isinstance(owner, str):
            raise TypeError(f"Expected owner to be a str, not {owner!r}")
        if description is not None and not isinstance(description, str):
            raise TypeError(f"Expected description to be a str, not {description!r}")

        super().__init__(iterable, key=lambda x: (x.album, x.disc, x.track, x.year or 0))

        kind = kind.strip().lower()
        if not kind:
            raise ValueError("Music kind must be provided")
        if year is not None and year <= 0:
            raise ValueError(f"Music year cannot be {year}")
        if total_tracks is not None and total_tracks <= 0:
            raise ValueError(f"Music total_tracks cannot be {total_tracks}")
        if total_discs is not None and total_discs <= 0:
            raise ValueError(f"Music total_discs cannot be {total_discs}")
        if total_duration is not None and total_duration < 0:
            raise ValueError(f"Music total_duration cannot be {total_duration}")

        self.kind = kind
        self.title = title.strip() if title else None
        self.artist = artist.strip() if artist else None
        self.year = year
        self.total_tracks = total_tracks
        self.total_discs = total_discs
        self.artwork_url = artwork_url.strip() if artwork_url else None
        self.total_duration = total_duration
        self.owner = owner.strip() if owner else None
        self.description = description.strip() if description else None

    def __str__(self) -> str:
        if not self:
            return super().__str__()
        first_song = self[0]
        artist = self.artist or getattr(first_song, "album_artist", None) or first_song.artist
        title = self.title or first_song.album
        year = self.year or first_song.year or "?"
        return f"{artist} - {title} ({year})"

    def tree(self, verbose: bool = False) -> Any:
        from unshackle.core.music.renderer import MusicRenderer

        return MusicRenderer().render(self, verbose=verbose)


class Album(Music):
    """Backward-compatible collection name for album-style music releases."""


__all__ = ("Song", "Music", "Album")
