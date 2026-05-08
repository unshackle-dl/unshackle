from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from unshackle.core.music.models import MusicDiscPlan, MusicDownloadPlan, MusicSongPlan, MusicTrackOption
from unshackle.core.titles.music import Music, Song


class MusicPlanner:
    """Build a service-neutral music list/download plan for native Music rendering."""

    def __init__(self, service: Any):
        self.service = service

    def build(self, music: Music) -> MusicDownloadPlan:
        first_song = music[0] if music else None
        first_data = first_song.data if first_song and isinstance(first_song.data, dict) else {}
        first_metadata = first_data.get("metadata") if isinstance(first_data.get("metadata"), dict) else {}
        plan = MusicDownloadPlan(
            kind=getattr(music, "kind", "music"),
            title=getattr(music, "title", None) or (first_song.album if first_song else ""),
            artist=getattr(music, "artist", None) or (first_song.album_artist or first_song.artist if first_song else ""),
            album_artist=(first_song.album_artist if first_song else "") or "",
            year=getattr(music, "year", None) or (first_song.year if first_song else None),
            released=self._first_text(
                getattr(music, "released", None),
                getattr(music, "release_date", None),
                first_data.get("release_date"),
                first_data.get("released_at"),
                first_metadata.get("release_date"),
                first_metadata.get("released_at"),
            ),
            genre=(first_song.genre if first_song else "") or "",
            label=(first_song.label if first_song else "") or "",
            artwork_url=getattr(music, "artwork_url", None),
            total_tracks=getattr(music, "total_tracks", None) or len(music),
            total_discs=getattr(music, "total_discs", None) or self._max_value(music, "disc"),
            total_duration=getattr(music, "total_duration", None) or self._sum_duration(music),
            quality_requested=self._quality_requested(music),
        )

        selected_options: list[MusicTrackOption] = []
        discs: dict[int, list[MusicSongPlan]] = defaultdict(list)
        for song in music:
            options = self._get_options(song)
            selected = options[0] if options else None
            if selected:
                selected_options.append(selected)
            discs[song.disc].append(
                MusicSongPlan(
                    song=song,
                    options=options,
                    selected=selected,
                )
            )

        for disc_number in sorted(discs):
            plan.discs.append(MusicDiscPlan(disc_number=disc_number, songs=discs[disc_number]))

        quality = self._quality_summary_from_options(selected_options)
        if quality:
            plan.quality_requested = quality

        return plan

    def _get_options(self, song: Song) -> list[MusicTrackOption]:
        provider = getattr(self.service, "get_music_track_options", None)
        if callable(provider):
            options = provider(song)
            if options:
                return options
        option = self._option_from_song(song)
        return [option] if option else []

    @staticmethod
    def _option_from_song(song: Song) -> MusicTrackOption | None:
        data = song.data if isinstance(song.data, dict) else {}
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        quality = MusicPlanner._first_text(data.get("quality"), metadata.get("quality"), metadata.get("quality_label"))
        duration = MusicPlanner._first_number(data.get("duration"), metadata.get("duration"))
        if not quality and duration is None:
            return None

        codec = MusicPlanner._codec_from_quality(quality)
        bit_depth, sample_rate = MusicPlanner._quality_numbers(quality)
        bitrate = MusicPlanner._bitrate_from_quality(quality)
        hires = bool((bit_depth and bit_depth > 16) or (sample_rate and sample_rate > 48000))
        lossless = codec in {"FLAC", "ALAC", "WAV", "AIFF"} or "lossless" in quality.lower()
        atmos = "atmos" in quality.lower() or bool(data.get("atmos") or metadata.get("atmos"))
        explicit = bool(getattr(song, "explicit", None) or data.get("explicit") or metadata.get("explicit"))

        return MusicTrackOption(
            codec=codec,
            bit_depth=bit_depth,
            sample_rate=sample_rate,
            bitrate=bitrate,
            channels=MusicPlanner._first_number(data.get("channels"), metadata.get("channels")),
            lossless=lossless,
            hires=hires,
            atmos=atmos,
            explicit=explicit,
            duration=int(duration) if duration is not None else None,
            quality_label=quality,
        )

    def _quality_requested(self, music: Music) -> str:
        service_quality = getattr(self.service, "quality", None)
        if service_quality:
            quality_map = {
                27: "Hi-Res Lossless",
                7: "Hi-Res Lossless",
                6: "Lossless",
                5: "MP3",
            }
            return quality_map.get(service_quality, str(service_quality))

        first_song = music[0] if music else None
        if not first_song:
            return ""
        data = first_song.data if isinstance(first_song.data, dict) else {}
        return self._first_text(data.get("quality"))

    @staticmethod
    def _first_text(*values: Any) -> str:
        for value in values:
            if value in (None, ""):
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _first_number(*values: Any) -> float | None:
        for value in values:
            if value in (None, ""):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _codec_from_quality(value: str) -> str:
        lowered = value.lower()
        for codec in ("flac", "alac", "aac", "mp3", "wav", "aiff"):
            if codec in lowered:
                return codec.upper()
        return ""

    @staticmethod
    def _quality_numbers(value: str) -> tuple[int | None, int | None]:
        bit_depth = None
        sample_rate = None
        bit_match = re.search(r"(?P<bits>\d+)\s*[- ]?bit", value.lower())
        if bit_match:
            bit_depth = int(bit_match.group("bits"))
        sample_match = re.search(r"(?P<rate>\d+(?:\.\d+)?)\s*k(?:hz)?", value.lower())
        if sample_match:
            sample_rate = int(float(sample_match.group("rate")) * 1000)
        return bit_depth, sample_rate

    @staticmethod
    def _bitrate_from_quality(value: str) -> int | None:
        match = re.search(r"(?P<rate>\d+)\s*kb/s", value.lower())
        if not match:
            return None
        return int(match.group("rate")) * 1000

    @staticmethod
    def _quality_summary_from_options(options: list[MusicTrackOption]) -> str:
        if not options:
            return ""

        codecs = {str(option.codec or "").upper() for option in options if option.codec}
        labels = [option.quality_label for option in options if option.quality_label]

        if any(option.atmos for option in options):
            return "Dolby Atmos"
        if any(option.hires and (option.lossless or str(option.codec or "").upper() in {"FLAC", "ALAC", "WAV", "AIFF"}) for option in options):
            return "Hi-Res Lossless"
        if any(option.lossless or str(option.codec or "").upper() in {"FLAC", "ALAC", "WAV", "AIFF"} for option in options):
            return "Lossless"
        if "AAC" in codecs:
            return "AAC"
        if "MP3" in codecs:
            return "MP3"
        return MusicPlanner._first_text(*labels)

    @staticmethod
    def _sum_duration(music: Music) -> int | None:
        total = 0
        for song in music:
            data = song.data if isinstance(song.data, dict) else {}
            value = MusicPlanner._first_number(data.get("duration"))
            total += int(value or 0)
        return total or None

    @staticmethod
    def _max_value(music: Music, attr: str) -> int | None:
        values = [getattr(song, attr, 0) or 0 for song in music]
        return max(values, default=0) or None


__all__ = ("MusicPlanner",)
