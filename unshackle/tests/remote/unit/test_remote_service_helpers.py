"""Unit tests for module-level helpers in unshackle.core.remote_service."""

from __future__ import annotations

from enum import Enum

import pytest

from unshackle.core.remote_service import (_build_title, _build_tracks, _deserialize_audio, _deserialize_subtitle,
                                           _deserialize_video, _enum_get, _match_track, _reconstruct_drm)
from unshackle.core.titles.episode import Episode
from unshackle.core.titles.movie import Movie
from unshackle.core.tracks import Audio, Subtitle, Video

pytestmark = pytest.mark.unit


class _Color(Enum):
    RED = 1
    BLUE = 2


def test_enum_get_known() -> None:
    assert _enum_get(_Color, "RED") is _Color.RED


def test_enum_get_unknown_returns_default() -> None:
    assert _enum_get(_Color, "PURPLE", default=_Color.BLUE) is _Color.BLUE


def test_enum_get_none_returns_default() -> None:
    assert _enum_get(_Color, None, default=_Color.RED) is _Color.RED


def test_deserialize_video_minimal() -> None:
    v = _deserialize_video({"id": "video-1", "codec": "AVC", "width": 1920, "height": 1080, "bitrate": 5000})
    assert isinstance(v, Video)
    assert v.id == "video-1"
    assert v.codec is Video.Codec.AVC
    assert v.bitrate == 5_000_000  # kbps -> bps
    assert v.width == 1920
    assert v.height == 1080
    assert v.range is Video.Range.SDR


def test_deserialize_video_unknown_codec_falls_back_to_none() -> None:
    v = _deserialize_video({"id": "v2", "codec": "MADE_UP", "width": 0, "height": 0})
    assert v.codec is None


def test_deserialize_audio_atmos_flag_sets_joc() -> None:
    a = _deserialize_audio({"id": "a1", "codec": "AAC", "atmos": True, "channels": 6, "bitrate": 256})
    assert isinstance(a, Audio)
    assert a.joc == 1
    assert a.channels == 6
    assert a.bitrate == 256_000


def test_deserialize_audio_no_atmos() -> None:
    a = _deserialize_audio({"id": "a2", "codec": "AAC", "channels": 2})
    assert a.joc == 0


def test_deserialize_subtitle_forced_flag() -> None:
    s = _deserialize_subtitle({"id": "s1", "codec": "WebVTT", "language": "en", "forced": True})
    assert isinstance(s, Subtitle)
    assert s.forced is True
    assert s.sdh is False


def test_deserialize_subtitle_sdh_flag() -> None:
    s = _deserialize_subtitle({"id": "s2", "codec": "WebVTT", "language": "en", "sdh": True})
    assert s.sdh is True
    assert s.forced is False


def test_reconstruct_drm_empty() -> None:
    assert _reconstruct_drm(None) == []
    assert _reconstruct_drm([]) == []


def test_reconstruct_drm_skips_entries_without_pssh() -> None:
    assert _reconstruct_drm([{"type": "widevine"}]) == []


def test_reconstruct_drm_invalid_pssh_silently_dropped() -> None:
    assert _reconstruct_drm([{"type": "widevine", "pssh": "not-real-pssh"}]) == []


def test_build_tracks_aggregates() -> None:
    data = {
        "video": [{"id": "v", "codec": "AVC", "width": 1280, "height": 720, "bitrate": 2500}],
        "audio": [{"id": "a", "codec": "AAC", "channels": 2, "bitrate": 128}],
        "subtitles": [{"id": "s", "codec": "WebVTT", "language": "en"}],
        "attachments": [],
    }
    t = _build_tracks(data)
    assert len(t.videos) == 1
    assert len(t.audio) == 1
    assert len(t.subtitles) == 1


def test_match_track_by_id() -> None:
    a = _deserialize_video({"id": "v1", "codec": "AVC", "width": 1920, "height": 1080})
    b = _deserialize_video({"id": "v2", "codec": "AVC", "width": 1280, "height": 720})
    remote = _deserialize_video({"id": "v2", "codec": "AVC", "width": 1280, "height": 720})
    assert _match_track(remote, [a, b]) is b


def test_match_track_by_attributes_when_id_missing() -> None:
    local = _deserialize_video({"id": "X", "codec": "AVC", "width": 1920, "height": 1080, "language": "en"})
    remote = _deserialize_video({"id": "Y", "codec": "AVC", "width": 1920, "height": 1080, "language": "en"})
    assert _match_track(remote, [local]) is local


def test_match_track_no_candidates_returns_none() -> None:
    remote = _deserialize_video({"id": "X", "codec": "AVC", "width": 1, "height": 1})
    assert _match_track(remote, []) is None


def test_build_title_movie() -> None:
    info = {"type": "movie", "id": "movie-0001", "name": "Foo", "year": 2024, "language": "en"}
    title = _build_title(info, "ATV", "fallback")
    assert isinstance(title, Movie)
    assert title.id == "movie-0001"
    assert title.name == "Foo"


def test_build_title_episode() -> None:
    info = {
        "type": "episode",
        "id": "ep-00001",
        "series_title": "Show",
        "season": 1,
        "number": 2,
        "name": "Pilot",
        "year": 2024,
        "language": "en",
    }
    title = _build_title(info, "ATV", "fallback")
    assert isinstance(title, Episode)
    assert title.season == 1
    assert title.number == 2
    assert title.name == "Pilot"


def test_build_title_falls_back_to_id_when_missing() -> None:
    title = _build_title({"type": "movie", "name": "x"}, "ATV", "fallback-id")
    assert title.id == "fallback-id"
