"""Unit tests for unshackle.core.api.handlers serializers + validators."""

from __future__ import annotations

import pytest
from langcodes import Language

from unshackle.core.api.handlers import (sanitize_log, serialize_audio_track, serialize_drm, serialize_subtitle_track,
                                         serialize_title, serialize_video_track, validate_download_parameters,
                                         validate_service)
from unshackle.core.titles.episode import Episode
from unshackle.core.titles.movie import Movie
from unshackle.core.tracks import Audio, Subtitle, Video
from unshackle.core.tracks.track import Track

pytestmark = pytest.mark.unit


class _FakeSvc:
    pass


def _video(**overrides) -> Video:
    base = dict(
        url="https://example.com/v.mpd",
        language=Language.get("en"),
        descriptor=Track.Descriptor.URL,
        codec=Video.Codec.AVC,
        range_=Video.Range.SDR,
        bitrate=5_000_000,
        width=1920,
        height=1080,
        fps=24,
        id_="video-001",
    )
    base.update(overrides)
    return Video(**base)


def _audio(**overrides) -> Audio:
    base = dict(
        url="https://example.com/a.mpd",
        language=Language.get("en"),
        descriptor=Track.Descriptor.URL,
        codec=Audio.Codec.AAC,
        bitrate=128_000,
        channels=2,
        joc=0,
        descriptive=False,
        id_="audio-001",
    )
    base.update(overrides)
    return Audio(**base)


def _subtitle(**overrides) -> Subtitle:
    base = dict(
        url="https://example.com/s.vtt",
        language=Language.get("en"),
        descriptor=Track.Descriptor.URL,
        codec=Subtitle.Codec.WebVTT,
        cc=False,
        sdh=False,
        forced=False,
        id_="sub-001",
    )
    base.update(overrides)
    return Subtitle(**base)


# ---------- sanitize_log ----------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("hello\nworld", "helloworld"),
        ("a\r\nb\x00c", "abc"),
        ("clean", "clean"),
        (12345, "12345"),
    ],
)
def test_sanitize_log(raw, expected: str) -> None:
    assert sanitize_log(raw) == expected


# ---------- serialize_title ----------


def test_serialize_title_movie() -> None:
    movie = Movie(id_="movie-0001", service=_FakeSvc, name="Title X", year=2024, language=Language.get("en"))
    d = serialize_title(movie)
    assert d["type"] == "movie"
    assert d["name"] == "Title X"
    assert d["year"] == 2024
    assert d["id"] == "movie-0001"
    assert d["language"] == "en"


def test_serialize_title_episode_named() -> None:
    ep = Episode(
        id_="ep-00001",
        service=_FakeSvc,
        title="My Show",
        season=2,
        number=3,
        name="Pilot",
        year=2024,
        language=Language.get("en"),
    )
    d = serialize_title(ep)
    assert d["type"] == "episode"
    assert d["series_title"] == "My Show"
    assert d["season"] == 2
    assert d["number"] == 3
    assert d["name"] == "Pilot"


def test_serialize_title_episode_unnamed_falls_back_to_number() -> None:
    ep = Episode(
        id_="ep-00002",
        service=_FakeSvc,
        title="Show",
        season=1,
        number=5,
        name=None,
        year=None,
        language=Language.get("en"),
    )
    d = serialize_title(ep)
    assert d["name"] == "Episode 05"


# ---------- serialize_video/audio/subtitle ----------


def test_serialize_video_track_basic() -> None:
    d = serialize_video_track(_video())
    assert d["id"] == "video-001"
    assert d["codec"] == "AVC"
    assert d["bitrate"] == 5000  # kbps
    assert d["resolution"] == "1920x1080"
    assert d["fps"] == 24
    assert d["range"] == "SDR"
    assert d["language"] == "en"
    assert d["drm"] is None
    assert "url" not in d


def test_serialize_video_track_include_url() -> None:
    d = serialize_video_track(_video(), include_url=True)
    assert d["url"] == "https://example.com/v.mpd"


def test_serialize_audio_track_basic() -> None:
    d = serialize_audio_track(_audio())
    assert d["id"] == "audio-001"
    assert d["codec"] == "AAC"
    assert d["bitrate"] == 128
    assert d["channels"] == 2
    assert d["descriptive"] is False


def test_serialize_subtitle_track_basic() -> None:
    d = serialize_subtitle_track(_subtitle(forced=True))
    assert d["id"] == "sub-001"
    assert d["codec"] == "WebVTT"
    assert d["forced"] is True
    assert d["sdh"] is False
    assert d["cc"] is False


# ---------- serialize_drm ----------


def test_serialize_drm_none_returns_none() -> None:
    assert serialize_drm(None) is None
    assert serialize_drm([]) is None


def test_serialize_drm_widevine_minimal() -> None:
    class _PSSH:
        def dumps(self) -> str:
            return "BASE64PSSH=="

    class _Widevine:
        def __init__(self) -> None:
            self._pssh = _PSSH()
            self.kids = ["00112233445566778899aabbccddeeff"]
            self.license_url = "https://lic.example.com/wv"

    out = serialize_drm(_Widevine())
    assert isinstance(out, list)
    assert len(out) == 1
    info = out[0]
    assert info["type"] == "_widevine"  # class name lowercased
    assert info["pssh"] == "BASE64PSSH=="
    assert info["kids"] == ["00112233445566778899aabbccddeeff"]
    assert info["license_url"] == "https://lic.example.com/wv"


def test_serialize_drm_playready_pssh_without_dumps_is_omitted() -> None:
    # pyplayready's PSSH exposes no dumps()/to_base64()/__bytes__; serialization
    # must omit the pssh field rather than emit an object repr.
    class _PSSH:
        pass

    class _PlayReady:
        def __init__(self) -> None:
            self._pssh = _PSSH()
            self.kids = ["00112233445566778899aabbccddeeff"]

    info = serialize_drm(_PlayReady())[0]
    assert "pssh" not in info
    assert info["kids"] == ["00112233445566778899aabbccddeeff"]


# ---------- validate_service ----------


def test_validate_service_unknown_returns_none() -> None:
    assert validate_service("NOPE_THIS_IS_NOT_REAL_") is None


# ---------- validate_download_parameters ----------


def test_validate_download_params_accepts_defaults() -> None:
    assert validate_download_parameters({}) is None


@pytest.mark.parametrize(
    "data, fragment",
    [
        ({"vcodec": "WUT"}, "Invalid vcodec"),
        ({"vcodec": 123}, "vcodec must be a string or list"),
        ({"acodec": "MP9"}, "Invalid acodec"),
        ({"sub_format": "doc"}, "Invalid sub_format"),
        ({"vbitrate": -1}, "vbitrate"),
        ({"abitrate": "no"}, "abitrate"),
        ({"vbitrate_range": "no-dash-but-letters"}, None),
        ({"vbitrate_range": "nope"}, "MIN-MAX"),
        ({"channels": -3}, "channels"),
        ({"workers": 0}, "workers"),
        ({"downloads": 0}, "downloads"),
        ({"video_only": True, "audio_only": True}, "exclusive"),
        ({"no_subs": True, "subs_only": True}, "no_subs and subs_only"),
        ({"no_audio": True, "audio_only": True}, "no_audio and audio_only"),
        ({"s_lang": ["en"], "require_subs": ["en"]}, "s_lang and require_subs"),
        ({"range": "UHD"}, "Invalid range"),
        ({"range": ["SDR", "UHD"]}, "Invalid range value"),
    ],
)
def test_validate_download_params_errors(data: dict, fragment) -> None:
    result = validate_download_parameters(data)
    if fragment is None:
        # A dash-containing string is valid syntactically per current rule
        assert result is None
    else:
        assert result is not None
        assert fragment in result


def test_validate_download_params_accepts_valid_values() -> None:
    assert (
        validate_download_parameters(
            {
                "vcodec": "H264,H265",
                "acodec": ["AAC", "EAC3"],
                "sub_format": "VTT",
                "vbitrate": 6000,
                "abitrate": 128,
                "vbitrate_range": "6000-7000",
                "abitrate_range": "96-192",
                "channels": 5.1,
                "workers": 8,
                "downloads": 2,
                "range": ["SDR", "HDR10"],
            }
        )
        is None
    )
