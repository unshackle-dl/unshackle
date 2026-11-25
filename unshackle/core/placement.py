"""Where each file a title produces goes: its folder, its name, and one rule for a name taken twice."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pymediainfo import MediaInfo

from unshackle.core.config import config
from unshackle.core.titles import Movie, Song
from unshackle.core.titles.episode import Episode
from unshackle.core.tracks import Audio, Subtitle, Video

SAMPLE_DIR = "Sample"


def template_type(title: Any) -> str:
    """Return the output template key of a title: ``series``, ``songs`` or ``movies``."""
    return "series" if isinstance(title, Episode) else "songs" if isinstance(title, Song) else "movies"


def sample_name(name: str, title: Any) -> str:
    """Return ``name`` with a SAMPLE tag, joined with the separator of the output template of the title."""
    return f"{name.rstrip()}{config.get_template_separator(template_type(title))}SAMPLE"


def track_file_suffix(track: Any) -> str:
    """The name part a track file saved on its own adds after the title: language and codec, or subtitle flags."""
    if isinstance(track, Video):
        return f".{track.codec.name if track.codec is not None else 'video'}"
    lang_suffix = f".{track.language}" if track.language else ""
    if isinstance(track, Audio):
        return f"{lang_suffix}.{track.codec.name if track.codec is not None else 'audio'}"
    if isinstance(track, Subtitle):
        return f"{lang_suffix}{'.forced' if track.forced else ''}{'.sdh' if track.sdh else ''}"
    return ""


def no_mux_extension(track: Any, path: Path, media_info: MediaInfo) -> str:
    """The extension of a track file saved on its own: .ts for MPEG-TS, .m4a for an audio track in MPEG-4."""
    container = media_info.general_tracks[0].format if media_info.general_tracks else None
    if container == "MPEG-TS":
        return ".ts"
    return ".m4a" if isinstance(track, Audio) and path.suffix == ".mp4" and container == "MPEG-4" else path.suffix


class Placement:
    """The final paths of the files of one title.

    A path this title already took gets the audio codec of a split-audio mux when the file exists,
    then a counter, so no file of the title overwrites another.
    """

    def __init__(self, title: Any, *, root: Path, no_folder: bool, no_source: bool, sample: bool) -> None:
        self.title = title
        self.root = root
        self.no_folder = no_folder
        self.no_source = no_source
        self.sample = sample
        self.used: set[Path] = set()

    @property
    def has_title_folder(self) -> bool:
        """Whether the files go in a folder named after the title."""
        return not self.no_folder and (
            isinstance(self.title, (Episode, Song))
            or bool(isinstance(self.title, Movie) and config.get_folder_template("movies"))
        )

    def folder(self, media_info: Optional[MediaInfo]) -> Path:
        """The folder of the title: the title folder when the title has one, then the Sample folder."""
        folder = self.root
        if media_info and self.has_title_folder:
            folder /= self.title.get_filename(media_info, show_service=not self.no_source, folder=True)
        return folder / (SAMPLE_DIR if self.sample else "")

    def name(self, media_info: MediaInfo) -> str:
        """The file name of the title, without the extension, with the SAMPLE tag for a sample."""
        name = self.title.get_filename(media_info, show_service=not self.no_source)
        return sample_name(name, self.title) if self.sample else name

    def claim(self, folder: Path, stem: str, suffix: str, audio_codec: Optional[Audio.Codec] = None) -> Path:
        """The final path for ``stem`` and ``suffix`` in ``folder``, which this title then holds."""
        folder.mkdir(parents=True, exist_ok=True)
        sep = config.get_template_separator(template_type(self.title))
        path = folder / f"{stem}{suffix}"
        if audio_codec and path.exists():
            stem = f"{stem.rstrip()}{sep}{audio_codec.name}"
            path = folder / f"{stem}{suffix}"
        i = 2
        while path in self.used:
            path = folder / f"{stem.rstrip()}{sep}{i}{suffix}"
            i += 1
        self.used.add(path)
        return path

    def track_file(self, folder: Path, track: Any, media_info: MediaInfo, stem: Optional[str] = None) -> Path:
        """The final path of a track file saved on its own, named after ``stem`` or else the title."""
        stem = f"{stem or self.name(media_info)}{track_file_suffix(track)}"
        return self.claim(folder, stem, no_mux_extension(track, track.path, media_info))
