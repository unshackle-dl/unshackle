from __future__ import annotations

import re
from typing import Any, Optional

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from unshackle.core.console import listing_panel
from unshackle.core.music.extract import first_text, format_duration
from unshackle.core.titles.music import Music, Song


class MusicRenderer:
    """Render a Music release for the CLI title listing.

    The album header and per-track lines are specific to music. The box around
    the tracklist comes from ``listing_panel``, so it restyles along with the
    track listing that core prints for every other title type.
    """

    def render(self, music: Music) -> RenderableType:
        header = self.render_header(music)
        tracks = self.render_tracks(music)
        if header:
            return Group(header, Text(""), tracks)
        return tracks

    def render_header(self, music: Music) -> Optional[Table]:
        if not music:
            return None

        first_song = music[0]
        data = first_song.data if isinstance(first_song.data, dict) else {}
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        title = music.title or first_song.album
        artist = music.artist or first_song.album_artist or first_song.artist
        year = getattr(music, "year", None) or first_song.year
        kind = self.display_kind(music.kind)
        explicit = any(bool(getattr(song, "explicit", None)) for song in music)
        total_tracks = getattr(music, "total_tracks", None) or len(music)
        total_discs = getattr(music, "total_discs", None) or self.max_value(music, "disc")
        released = self.format_release_date(
            self.first_value(
                getattr(music, "released", None),
                getattr(music, "release_date", None),
                data.get("release_date"),
                data.get("released_at"),
                metadata.get("release_date"),
                metadata.get("released_at"),
            )
        )
        length = self.format_total_duration(getattr(music, "total_duration", None) or self.sum_duration(music))
        quality = self.quality_summary(
            first_text(
                getattr(music, "quality", None),
                data.get("quality"),
                metadata.get("quality"),
                metadata.get("quality_label"),
            ),
            lossless=self.as_bool(self.first_value(data.get("lossless"), metadata.get("lossless"))),
            hires=self.as_bool(self.first_value(data.get("hires"), metadata.get("hires"))),
        )

        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bright_black", no_wrap=True)
        grid.add_column()

        # values are service data, so they stay Text and never get parsed as markup
        grid.add_row("Title", Text(str(title)))
        grid.add_row("Artist", Text(str(artist)))
        grid.add_row("Type", self.kind_text(kind, explicit=explicit))
        if released:
            grid.add_row("Released", Text(released))
        if year:
            grid.add_row("Year", Text(str(year)))
        grid.add_row("Tracks", Text(str(total_tracks)))
        if total_discs and total_discs > 1:
            grid.add_row("Discs", Text(str(total_discs)))
        if length:
            grid.add_row("Length", Text(length))
        if quality:
            grid.add_row("Quality", Text(quality))
        if first_song.genre:
            grid.add_row("Genre", Text(first_song.genre))
        if first_song.label:
            grid.add_row("Label", Text(first_song.label))

        return grid

    def render_tracks(self, music: Music) -> Panel:
        total = len(music)
        track_label = "Track" if total == 1 else "Tracks"
        tree = Tree(f"[repr.number]{total}[/] {track_label}", guide_style="bright_black")

        for song in music:
            node = tree.add(self.song_line(song, music))
            option = self.option_from_song(song)
            if option:
                node.add(option)

        return listing_panel(tree, "Tracklist")

    @staticmethod
    def display_kind(kind: Any) -> str:
        text = str(kind or "music").strip()
        key = re.sub(r"[^a-z0-9]+", "", text.lower())
        labels = {
            "album": "Album",
            "single": "Single",
            "ep": "EP",
            "epsingle": "Single",
            "playlist": "Playlist",
            "compilation": "Compilation",
            "live": "Live",
            "download": "Download",
            "other": "Other",
            "track": "Track",
            "music": "Music",
        }
        if key in labels:
            return labels[key]
        return text.replace("_", " ").replace("-", " ").title()

    def song_line(self, song: Song, music: Music) -> Text:
        number = f"{song.disc}.{song.track:02}" if song.disc > 1 else f"{song.track:02}"
        line = Text()
        line.append(number, style="repr.number")
        line.append("   ")
        line.append(song.name, style="rule.text")
        kind = getattr(music, "kind", "").lower()
        release_artist = getattr(music, "artist", None) or getattr(music, "album_artist", None)
        if kind == "playlist" and song.artist and song.artist != release_artist:
            line.append(f" - {song.artist}", style="bright_black")
        return line

    def option_from_song(self, song: Song) -> Text:
        data = song.data if isinstance(song.data, dict) else {}
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}

        quality = first_text(data.get("quality"), metadata.get("quality"), metadata.get("quality_label"))
        raw_duration = self.first_value(data.get("duration"), metadata.get("duration"))
        # an already-formatted duration ("3:45") does not parse as seconds, so show it as given
        duration = format_duration(raw_duration) or first_text(raw_duration)
        reason = first_text(
            data.get("unavailable_reason"),
            data.get("skip_reason"),
            metadata.get("unavailable_reason"),
            metadata.get("skip_reason"),
        )
        if reason:
            return Text.assemble(("Skipped:", "yellow"), f" {reason}")

        badges = []
        if song.explicit:
            badges.append(("E", "bold bright_red"))
        if self.as_bool(self.first_value(data.get("atmos"), metadata.get("atmos"))):
            badges.append(("Atmos", "magenta"))
        if self.is_hires_quality(quality):
            badges.append(("Hi-Res", "gold1"))

        details = []
        if quality:
            details.append(quality)
        if duration:
            details.append(duration)
        if not details and not badges:
            return Text()
        return self.format_option_text(details, badges)

    @staticmethod
    def kind_text(kind: str, *, explicit: bool = False) -> Text:
        text = Text(str(kind))
        if explicit:
            text.append(" Explicit", style="bold red")
        return text

    @staticmethod
    def format_option_text(parts: list[str], badges: list[tuple[str, str]]) -> Text:
        text = Text(style="text2")
        first = True
        for part in parts:
            if not part:
                continue
            if not first:
                text.append(" | ")
            text.append(str(part))
            first = False
        for badge, style in badges:
            if not first:
                text.append(" | ")
            text.append(badge, style=style)
            first = False
        return text

    @staticmethod
    def first_value(*values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return None

    @staticmethod
    def as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y"}
        return bool(value)

    @staticmethod
    def format_total_duration(value: Any) -> str:
        if value in (None, ""):
            return ""
        try:
            total_seconds = int(float(value))
        except (TypeError, ValueError):
            return str(value).strip()
        if total_seconds <= 0:
            return ""
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes:02}m {seconds:02}s"
        return f"{minutes}m {seconds:02}s"

    @staticmethod
    def format_release_date(value: Any) -> str:
        if value in (None, ""):
            return ""
        text = str(value).strip()
        match = re.fullmatch(r"(?P<year>\d{4})(?:-(?P<month>\d{2})-(?P<day>\d{2}))?.*", text)
        if not match or not match.group("month"):
            return text

        try:
            year = int(match.group("year"))
            month = int(match.group("month"))
            day = int(match.group("day"))
        except (TypeError, ValueError):
            return text

        month_names = (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        )
        if month < 1 or month > 12 or day < 1 or day > 31:
            return text
        return f"{month_names[month - 1]} {day}, {year}"

    @classmethod
    def quality_summary(cls, value: Any, *, lossless: bool = False, hires: bool = False) -> str:
        text = str(value or "").strip()
        lowered = text.lower()
        if not text:
            if hires and lossless:
                return "Hi-Res Lossless"
            if lossless:
                return "Lossless"
            return ""
        if "atmos" in lowered:
            return "Dolby Atmos"
        if any(codec in lowered for codec in ("flac", "alac", "wav", "aiff")):
            return "Hi-Res Lossless" if cls.is_hires_quality(text) else "Lossless"
        if "lossless" in lowered:
            return "Hi-Res Lossless" if "hi-res" in lowered or hires else "Lossless"
        if "aac" in lowered:
            return "AAC"
        if "mp3" in lowered:
            return "MP3"
        return text

    @staticmethod
    def is_hires_quality(value: str) -> bool:
        lowered = value.lower()
        bit_depth = None
        sample_rate = None

        bit_match = re.search(r"(?P<bits>\d+)\s*[- ]?bit", lowered)
        if bit_match:
            bit_depth = int(bit_match.group("bits"))

        sample_match = re.search(r"(?P<rate>\d+(?:\.\d+)?)\s*k(?:hz)?", lowered)
        if sample_match:
            sample_rate = float(sample_match.group("rate"))

        return bool((bit_depth and bit_depth > 16) or (sample_rate and sample_rate > 48))

    @staticmethod
    def sum_duration(music: Music) -> int:
        total = 0
        for song in music:
            data = song.data if isinstance(song.data, dict) else {}
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            value = MusicRenderer.first_value(data.get("duration"), metadata.get("duration"))
            try:
                total += int(float(value or 0))
            except (TypeError, ValueError):
                continue
        return total

    @staticmethod
    def max_value(music: Music, attr: str) -> int:
        values = [getattr(song, attr, 0) or 0 for song in music]
        return max(values, default=0)


__all__ = ("MusicRenderer",)
