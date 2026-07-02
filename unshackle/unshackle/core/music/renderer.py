from __future__ import annotations

import re
from typing import Any, Optional

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from unshackle.core.music.models import MusicDownloadPlan, MusicTrackOption
from unshackle.core.titles.music import Music, Song


class MusicRenderer:
    """Render native Music title containers for the CLI."""

    COMPACT_TRACK_LIMIT = 8

    def render(self, music: Music, *, verbose: bool = False) -> RenderableType:
        header = self.render_header(music)
        tracks = self.render_tracks(music, verbose=verbose)
        if header:
            return Group(header, Text(""), tracks)
        return tracks

    def render_plan(self, plan: MusicDownloadPlan, *, verbose: bool = True) -> RenderableType:
        header = self.render_plan_header(plan)
        tracks = self.render_plan_tracks(plan, verbose=verbose)
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
        total_discs = getattr(music, "total_discs", None) or self._max_value(music, "disc")
        released = self._format_release_date(
            self._first_value(
                getattr(music, "released", None),
                getattr(music, "release_date", None),
                data.get("release_date"),
                data.get("released_at"),
                metadata.get("release_date"),
                metadata.get("released_at"),
            )
        )
        length = self._format_total_duration(
            getattr(music, "total_duration", None) or self._sum_duration(music)
        )
        quality = self._quality_summary(
            self._first_text(
                getattr(music, "quality", None),
                data.get("quality"),
                metadata.get("quality"),
                metadata.get("quality_label"),
            ),
            lossless=self._as_bool(self._first_value(data.get("lossless"), metadata.get("lossless"))),
            hires=self._as_bool(self._first_value(data.get("hires"), metadata.get("hires"))),
        )

        grid = self._metadata_grid()

        grid.add_row(Text("Title", style="bright_black"), Text(str(title)))
        grid.add_row(Text("Artist", style="bright_black"), Text(str(artist)))
        grid.add_row(Text("Type", style="bright_black"), self._kind_text(kind, explicit=explicit))
        if released:
            grid.add_row(Text("Released", style="bright_black"), Text(released))
        if year:
            grid.add_row(Text("Year", style="bright_black"), Text(str(year)))
        grid.add_row(Text("Tracks", style="bright_black"), Text(str(total_tracks)))
        if total_discs and total_discs > 1:
            grid.add_row(Text("Discs", style="bright_black"), Text(str(total_discs)))
        if length:
            grid.add_row(Text("Length", style="bright_black"), Text(length))
        if quality:
            grid.add_row(Text("Quality", style="bright_black"), Text(quality))
        if first_song.genre:
            grid.add_row(Text("Genre", style="bright_black"), Text(first_song.genre))
        if first_song.label:
            grid.add_row(Text("Label", style="bright_black"), Text(first_song.label))

        return grid

    def render_tracks(self, music: Music, *, verbose: bool = False) -> Panel:
        total = len(music)
        track_label = "Track" if total == 1 else "Tracks"
        tree = Tree(f"[repr.number]{total}[/] {track_label}", guide_style="bright_black")

        visible_songs = list(music)
        if not verbose and len(visible_songs) > self.COMPACT_TRACK_LIMIT:
            visible_songs = visible_songs[: self.COMPACT_TRACK_LIMIT]

        for song in visible_songs:
            node = tree.add(self._song_line(song, music), guide_style="bright_black")
            option = self._option_from_song(song)
            if option:
                node.add(option, guide_style="bright_black")

        hidden = total - len(visible_songs)
        if hidden > 0:
            suffix = "s" if hidden != 1 else ""
            tree.add(f"[bright_black]... {hidden} more track{suffix}[/]", guide_style="bright_black")

        return Panel(tree, title="Available Tracks")

    def render_plan_header(self, plan: MusicDownloadPlan) -> Optional[Table]:
        title = plan.title
        artist = plan.artist or plan.album_artist
        kind = self.display_kind(plan.kind)
        explicit = any(
            bool(getattr(song_plan.song, "explicit", None) or (song_plan.selected and song_plan.selected.explicit))
            for disc in plan.discs
            for song_plan in disc.songs
        )
        released = self._format_release_date(getattr(plan, "released", None) or getattr(plan, "release_date", None))
        length = self._format_total_duration(plan.total_duration)
        quality = self._quality_summary(plan.quality_requested)

        grid = self._metadata_grid()
        if title:
            grid.add_row(Text("Title", style="bright_black"), Text(str(title)))
        if artist:
            grid.add_row(Text("Artist", style="bright_black"), Text(str(artist)))
        grid.add_row(Text("Type", style="bright_black"), self._kind_text(kind, explicit=explicit))
        if released:
            grid.add_row(Text("Released", style="bright_black"), Text(released))
        if plan.year:
            grid.add_row(Text("Year", style="bright_black"), Text(str(plan.year)))
        if plan.total_tracks:
            grid.add_row(Text("Tracks", style="bright_black"), Text(str(plan.total_tracks)))
        if plan.total_discs and plan.total_discs > 1:
            grid.add_row(Text("Discs", style="bright_black"), Text(str(plan.total_discs)))
        if length:
            grid.add_row(Text("Length", style="bright_black"), Text(length))
        if quality:
            grid.add_row(Text("Quality", style="bright_black"), Text(quality))
        if plan.genre:
            grid.add_row(Text("Genre", style="bright_black"), Text(str(plan.genre)))
        if plan.label:
            grid.add_row(Text("Label", style="bright_black"), Text(str(plan.label)))
        return grid

    def render_plan_tracks(self, plan: MusicDownloadPlan, *, verbose: bool = True) -> Panel:
        songs = [song_plan for disc in plan.discs for song_plan in disc.songs]
        total = len(songs)
        track_label = "Track" if total == 1 else "Tracks"
        tree = Tree(f"[repr.number]{total}[/] {track_label}", guide_style="bright_black")

        visible_songs = songs
        if not verbose and len(visible_songs) > self.COMPACT_TRACK_LIMIT:
            visible_songs = visible_songs[: self.COMPACT_TRACK_LIMIT]

        for song_plan in visible_songs:
            node = tree.add(self._song_line(song_plan.song, plan), guide_style="bright_black")
            if song_plan.skip_reason:
                node.add(f"[yellow]Skipped:[/] {escape(song_plan.skip_reason)}", guide_style="bright_black")
                continue
            for option in song_plan.options:
                node.add(self._option_line(option), guide_style="bright_black")

        hidden = total - len(visible_songs)
        if hidden > 0:
            suffix = "s" if hidden != 1 else ""
            tree.add(f"[bright_black]... {hidden} more track{suffix}[/]", guide_style="bright_black")

        return Panel(tree, title="Available Tracks")

    @staticmethod
    def _metadata_grid() -> Table:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="orchid1", no_wrap=True)
        grid.add_column()
        return grid

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

    def _song_line(self, song: Song, music: Music | MusicDownloadPlan) -> Text:
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

    def _option_from_song(self, song: Song) -> Text | str:
        data = song.data if isinstance(song.data, dict) else {}
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}

        quality = self._first_text(data.get("quality"), metadata.get("quality"), metadata.get("quality_label"))
        duration = self._format_duration(self._first_value(data.get("duration"), metadata.get("duration")))
        reason = self._first_text(
            data.get("unavailable_reason"),
            data.get("skip_reason"),
            metadata.get("unavailable_reason"),
            metadata.get("skip_reason"),
        )
        if reason:
            return f"[yellow]Skipped:[/] {escape(reason)}"

        badges = []
        if song.explicit:
            badges.append(("E", "bold bright_red"))
        if self._as_bool(self._first_value(data.get("atmos"), metadata.get("atmos"))):
            badges.append(("Atmos", "magenta"))
        if self._is_hires_quality(quality):
            badges.append(("Hi-Res", "gold1"))

        details = []
        if quality:
            details.append(quality)
        if duration:
            details.append(duration)
        if not details and not badges:
            return ""
        return self._format_option_text(details, badges)

    def _option_line(self, option: MusicTrackOption) -> Text:
        parts = []
        codec = str(option.codec or "").strip()
        if codec:
            parts.append(f"[{codec}]")
        if option.quality_label:
            parts.extend(self._split_quality_label(option.quality_label, codec))
        elif option.bit_depth and option.sample_rate:
            parts.append(f"{option.bit_depth}-bit/{self._format_sample_rate(option.sample_rate)}")
        elif option.bitrate:
            parts.append(f"{int(option.bitrate / 1000)} kb/s")
        if option.channels:
            parts.append(self._format_channels(option.channels))
        if option.duration:
            parts.append(self._format_duration(option.duration))
        badges = []
        if option.explicit:
            badges.append(("E", "bold bright_red"))
        if option.atmos:
            badges.append(("Atmos", "magenta"))
        if self._is_cd_option(option):
            badges.append(("CD", "yellow1"))
        if option.hires:
            badges.append(("Hi-Res", "gold1"))
        return self._format_option_text(parts, badges)

    @staticmethod
    def _kind_text(kind: str, *, explicit: bool = False) -> Text:
        text = Text(str(kind))
        if explicit:
            text.append(" Explicit", style="bold red")
        return text

    @staticmethod
    def _format_option_text(parts: list[str], badges: list[tuple[str, str]]) -> Text:
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
    def _format_option_parts(parts: list[str]) -> str:
        if not parts:
            return ""
        return " | ".join(escape(part) for part in parts if part)

    @staticmethod
    def _split_quality_label(value: str, codec: str = "") -> list[str]:
        text = str(value or "").strip()
        if not text:
            return []
        if codec and text.lower().startswith(codec.lower()):
            text = text[len(codec) :].strip()
        return [text] if text else []

    @staticmethod
    def _is_cd_option(option: MusicTrackOption) -> bool:
        codec = str(option.codec or "").upper()
        if codec not in {"FLAC", "ALAC", "WAV", "AIFF"}:
            return False
        if option.bit_depth == 16 and option.sample_rate in {44100, 44100.0}:
            return True
        return "16-bit/44.1" in str(option.quality_label or "").lower()

    @staticmethod
    def _format_sample_rate(value: Any) -> str:
        try:
            sample_rate = float(value)
        except (TypeError, ValueError):
            return str(value).strip()
        if sample_rate >= 1000:
            sample_rate /= 1000
        if sample_rate.is_integer():
            return f"{int(sample_rate)} kHz"
        return f"{sample_rate:g} kHz"

    @staticmethod
    def _format_channels(value: Any) -> str:
        try:
            channels = float(value)
        except (TypeError, ValueError):
            return str(value).strip()
        if channels == 1:
            return "Mono"
        if channels == 2:
            return "Stereo"
        if channels.is_integer():
            return f"{int(channels)}.0"
        return f"{channels:g}"

    @staticmethod
    def _first_text(*values: Any) -> str:
        for value in values:
            if value is None:
                continue
            if isinstance(value, dict):
                for key in ("label", "name", "title", "display_name", "value"):
                    nested = value.get(key)
                    if nested:
                        return str(nested).strip()
            elif isinstance(value, (list, tuple)):
                text = MusicRenderer._first_text(*value)
                if text:
                    return text
            else:
                text = str(value).strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _first_value(*values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return None

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y"}
        return bool(value)

    @staticmethod
    def _format_duration(value: Any) -> str:
        if value in (None, ""):
            return ""
        try:
            seconds = int(float(value))
        except (TypeError, ValueError):
            return str(value).strip()
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02}:{seconds:02}"
        return f"{minutes}:{seconds:02}"

    @staticmethod
    def _format_total_duration(value: Any) -> str:
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
    def _format_release_date(value: Any) -> str:
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
    def _quality_summary(cls, value: Any, *, lossless: bool = False, hires: bool = False) -> str:
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
            return "Hi-Res Lossless" if cls._is_hires_quality(text) else "Lossless"
        if "lossless" in lowered:
            return "Hi-Res Lossless" if "hi-res" in lowered or hires else "Lossless"
        if "aac" in lowered:
            return "AAC"
        if "mp3" in lowered:
            return "MP3"
        return text

    @staticmethod
    def _is_hires_quality(value: str) -> bool:
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
    def _sum_duration(music: Music) -> int:
        total = 0
        for song in music:
            data = song.data if isinstance(song.data, dict) else {}
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            value = MusicRenderer._first_value(data.get("duration"), metadata.get("duration"))
            try:
                total += int(float(value or 0))
            except (TypeError, ValueError):
                continue
        return total

    @staticmethod
    def _max_value(music: Music, attr: str) -> int:
        values = [getattr(song, attr, 0) or 0 for song in music]
        return max(values, default=0)


__all__ = ("MusicRenderer",)
