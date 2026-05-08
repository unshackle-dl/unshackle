from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pymediainfo import MediaInfo

from unshackle.core import binaries


class MusicAudioIntegrityError(Exception):
    """Raised when a downloaded music audio file cannot be trusted."""


@dataclass
class MusicAudioIntegrityResult:
    path: Path
    size: int
    codec: str = ""
    duration: Optional[float] = None
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[float] = None
    flac_tested: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "size": self.size,
            "codec": self.codec,
            "duration": self.duration,
            "bit_depth": self.bit_depth,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "flac_tested": self.flac_tested,
            "warnings": self.warnings,
        }


def verify_music_audio(path: Path, *, song: Any = None, track: Any = None, media_info: Optional[MediaInfo] = None) -> MusicAudioIntegrityResult:
    """Verify a downloaded music audio file after it is playable/decrypted."""
    path = Path(path)
    if not path.exists():
        raise MusicAudioIntegrityError(f"Audio file is missing: {path}")
    size = path.stat().st_size
    if size <= 3:
        raise MusicAudioIntegrityError(f"Audio file is empty: {path}")

    media_info = media_info or MediaInfo.parse(path)
    audio_track = next(iter(media_info.audio_tracks or []), None)
    if not audio_track:
        raise MusicAudioIntegrityError(f"MediaInfo could not find an audio stream in: {path.name}")

    duration = _duration_seconds(getattr(audio_track, "duration", None))
    expected_duration = _expected_duration(song, track)
    warnings: list[str] = []
    if expected_duration and duration:
        tolerance = max(3.0, expected_duration * 0.02)
        if abs(duration - expected_duration) > tolerance:
            raise MusicAudioIntegrityError(
                f"{path.name} duration mismatch: expected {expected_duration:.0f}s, got {duration:.0f}s"
            )

    result = MusicAudioIntegrityResult(
        path=path,
        size=size,
        codec=_first_text(
            getattr(audio_track, "format", None),
            getattr(audio_track, "commercial_name", None),
            getattr(audio_track, "codec_id", None),
        ),
        duration=duration,
        bit_depth=_first_int(getattr(audio_track, "bit_depth", None)),
        sample_rate=_first_int(getattr(audio_track, "sampling_rate", None)),
        channels=_first_float(
            getattr(audio_track, "channel_s", None),
            getattr(audio_track, "channel_s_original", None),
            getattr(track, "channels", None),
        ),
        warnings=warnings,
    )

    expected_size = _expected_size(track)
    if expected_size and result.size != expected_size:
        result.warnings.append(f"Size differs from service metadata: expected {expected_size}, got {result.size}")

    if path.suffix.lower() == ".flac":
        flac = binaries.find("flac")
        if flac:
            completed = subprocess.run(
                [str(flac), "-t", "-s", str(path)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise MusicAudioIntegrityError(f"FLAC stream test failed for {path.name}: {detail}")
            result.flac_tested = True
        else:
            result.warnings.append("flac binary not available; FLAC stream test skipped")

    return result


def _expected_duration(song: Any, track: Any) -> Optional[float]:
    data = getattr(song, "data", None)
    if isinstance(data, dict):
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        value = _first_float(data.get("duration"), metadata.get("duration"))
        if value:
            return value
    track_data = getattr(track, "data", None)
    if isinstance(track_data, dict):
        metadata = track_data.get("metadata") if isinstance(track_data.get("metadata"), dict) else {}
        value = _first_float(track_data.get("duration"), metadata.get("duration"))
        if value:
            return value
    return None


def _expected_size(track: Any) -> Optional[int]:
    data = getattr(track, "data", None)
    if not isinstance(data, dict):
        return None
    sources = [
        data,
        data.get("file_info") if isinstance(data.get("file_info"), dict) else {},
        data.get("audio_info") if isinstance(data.get("audio_info"), dict) else {},
        data.get("source_info") if isinstance(data.get("source_info"), dict) else {},
        data.get("stream_info") if isinstance(data.get("stream_info"), dict) else {},
        data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
    ]
    for source in sources:
        value = _first_int(
            source.get("content_length"),
            source.get("contentLength"),
            source.get("file_size"),
            source.get("filesize"),
            source.get("size"),
        )
        if value:
            return value
    return None


def _duration_seconds(value: Any) -> Optional[float]:
    number = _first_float(value)
    if number is None:
        return None
    if number > 1000:
        return number / 1000
    return number


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        if value in (None, "", [], {}):
            continue
        text = str(value).strip().replace(" ", "")
        try:
            return float(text)
        except (TypeError, ValueError):
            continue
    return None


def _first_int(*values: Any) -> Optional[int]:
    value = _first_float(*values)
    return int(value) if value is not None else None


__all__ = ("MusicAudioIntegrityError", "MusicAudioIntegrityResult", "verify_music_audio")
