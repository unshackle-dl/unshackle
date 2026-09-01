"""Unit tests for module-level helpers in unshackle.core.remote_service."""

from __future__ import annotations

from enum import Enum

import pytest

from unshackle.core.remote_service import (
    build_title,
    build_tracks,
    deserialize_audio,
    deserialize_subtitle,
    deserialize_video,
    enum_get,
    match_track,
    reconstruct_drm,
)
from unshackle.core.titles.episode import Episode
from unshackle.core.titles.movie import Movie
from unshackle.core.tracks import Audio, Subtitle, Video

pytestmark = pytest.mark.unit


class _Color(Enum):
    RED = 1
    BLUE = 2


def test_enum_get_known() -> None:
    assert enum_get(_Color, "RED") is _Color.RED


def test_enum_get_unknown_returns_default() -> None:
    assert enum_get(_Color, "PURPLE", default=_Color.BLUE) is _Color.BLUE


def test_enum_get_none_returns_default() -> None:
    assert enum_get(_Color, None, default=_Color.RED) is _Color.RED


def test_deserialize_video_minimal() -> None:
    v = deserialize_video({"id": "video-1", "codec": "AVC", "width": 1920, "height": 1080, "bitrate": 5000})
    assert isinstance(v, Video)
    assert v.id == "video-1"
    assert v.codec is Video.Codec.AVC
    assert v.bitrate == 5_000_000  # kbps -> bps
    assert v.width == 1920
    assert v.height == 1080
    assert v.range is Video.Range.SDR


def test_deserialize_video_unknown_codec_falls_back_to_none() -> None:
    v = deserialize_video({"id": "v2", "codec": "MADE_UP", "width": 0, "height": 0})
    assert v.codec is None


def test_deserialize_audio_atmos_flag_without_a_count() -> None:
    a = deserialize_audio({"id": "a1", "codec": "AAC", "atmos": True, "channels": 6, "bitrate": 256})
    assert isinstance(a, Audio)
    assert a.atmos is True
    assert a.joc is None
    assert a.channels == 6
    assert a.bitrate == 256_000


def test_deserialize_audio_atmos_keeps_a_real_joc_count() -> None:
    a = deserialize_audio({"id": "a3", "codec": "EC3", "atmos": True, "joc": 16, "channels": 6})
    assert a.atmos is True
    assert a.joc == 16


def test_deserialize_audio_no_atmos() -> None:
    a = deserialize_audio({"id": "a2", "codec": "AAC", "channels": 2})
    assert a.atmos is False
    assert not a.joc


def test_deserialize_subtitle_forced_flag() -> None:
    s = deserialize_subtitle({"id": "s1", "codec": "WebVTT", "language": "en", "forced": True})
    assert isinstance(s, Subtitle)
    assert s.forced is True
    assert s.sdh is False


def test_deserialize_subtitle_sdh_flag() -> None:
    s = deserialize_subtitle({"id": "s2", "codec": "WebVTT", "language": "en", "sdh": True})
    assert s.sdh is True
    assert s.forced is False


def test_reconstruct_drm_empty() -> None:
    assert reconstruct_drm(None) == []
    assert reconstruct_drm([]) == []


def test_reconstruct_drm_skips_entries_without_pssh() -> None:
    assert reconstruct_drm([{"type": "widevine"}]) == []


def test_reconstruct_drm_invalid_pssh_silently_dropped() -> None:
    assert reconstruct_drm([{"type": "widevine", "pssh": "not-real-pssh"}]) == []


def test_build_tracks_aggregates() -> None:
    data = {
        "video": [{"id": "v", "codec": "AVC", "width": 1280, "height": 720, "bitrate": 2500}],
        "audio": [{"id": "a", "codec": "AAC", "channels": 2, "bitrate": 128}],
        "subtitles": [{"id": "s", "codec": "WebVTT", "language": "en"}],
        "attachments": [],
    }
    t = build_tracks(data)
    assert len(t.videos) == 1
    assert len(t.audio) == 1
    assert len(t.subtitles) == 1


def test_build_tracks_restores_drm_preference() -> None:
    data = {
        "video": [{"id": "v", "codec": "AVC", "width": 1280, "height": 720, "drm_preference": "playready"}],
        "audio": [{"id": "a", "codec": "AAC", "channels": 2, "drm_preference": "wv"}],
        "subtitles": [],
    }
    t = build_tracks(data)
    assert t.videos[0].drm_preference == "playready"
    assert t.audio[0].drm_preference == "wv"


def test_build_tracks_ignores_unknown_drm_preference() -> None:
    data = {"video": [{"id": "v", "codec": "AVC", "width": 1, "height": 1, "drm_preference": "fairplay"}]}
    t = build_tracks(data)
    assert t.videos[0].drm_preference is None


def test_match_track_by_id() -> None:
    a = deserialize_video({"id": "v1", "codec": "AVC", "width": 1920, "height": 1080})
    b = deserialize_video({"id": "v2", "codec": "AVC", "width": 1280, "height": 720})
    remote = deserialize_video({"id": "v2", "codec": "AVC", "width": 1280, "height": 720})
    assert match_track(remote, [a, b]) is b


def test_match_track_by_attributes_when_id_missing() -> None:
    local = deserialize_video({"id": "X", "codec": "AVC", "width": 1920, "height": 1080, "language": "en"})
    remote = deserialize_video({"id": "Y", "codec": "AVC", "width": 1920, "height": 1080, "language": "en"})
    assert match_track(remote, [local]) is local


def test_match_track_by_attributes_keeps_range_and_bitrate_apart() -> None:
    sdr = deserialize_video(
        {"id": "a", "codec": "HEVC", "width": 1920, "height": 960, "language": "en", "range": "SDR", "bitrate": 8000000}
    )
    hdr = deserialize_video(
        {
            "id": "b",
            "codec": "HEVC",
            "width": 1920,
            "height": 960,
            "language": "en",
            "range": "HDR10",
            "bitrate": 5000000,
        }
    )
    remote = deserialize_video(
        {
            "id": "z",
            "codec": "HEVC",
            "width": 1920,
            "height": 960,
            "language": "en",
            "range": "HDR10",
            "bitrate": 5000000,
        }
    )
    assert match_track(remote, [sdr, hdr]) is hdr


def test_match_track_no_candidates_returns_none() -> None:
    remote = deserialize_video({"id": "X", "codec": "AVC", "width": 1, "height": 1})
    assert match_track(remote, []) is None


def test_build_title_movie() -> None:
    info = {"type": "movie", "id": "movie-0001", "name": "Foo", "year": 2024, "language": "en"}
    title = build_title(info, "ATV", "fallback")
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
    title = build_title(info, "ATV", "fallback")
    assert isinstance(title, Episode)
    assert title.season == 1
    assert title.number == 2
    assert title.name == "Pilot"


def test_build_title_falls_back_to_id_when_missing() -> None:
    title = build_title({"type": "movie", "name": "x"}, "ATV", "fallback-id")
    assert title.id == "fallback-id"


def test_credential_cache_digests_cover_common_key_schemes() -> None:
    import hashlib

    from unshackle.core.credential import Credential
    from unshackle.core.remote_service import credential_cache_digests

    cred = Credential("user@example.com", "hunter2")
    digests = credential_cache_digests(cred)
    assert cred.sha1 in digests
    assert hashlib.sha1(cred.username.encode()).hexdigest() in digests
    assert hashlib.md5(cred.username.encode()).hexdigest() in digests


def test_cache_stem_relevance_filters_foreign_digests_and_profiles() -> None:
    import hashlib

    from unshackle.core.credential import Credential
    from unshackle.core.remote_service import cache_stem_is_relevant, credential_cache_digests

    active = Credential("us@example.com", "pw1")
    other = Credential("uk@example.com", "pw2")
    allowed = credential_cache_digests(active)
    foreign = {"uk"}

    assert cache_stem_is_relevant(f"tokens_{active.sha1}", allowed, "us", foreign)
    assert not cache_stem_is_relevant(f"tokens_{other.sha1}", allowed, "us", foreign)
    username_hash = hashlib.sha1(active.username.encode()).hexdigest()
    assert cache_stem_is_relevant(f"device_id_{username_hash}", allowed, "us", foreign)
    # Service-global state carries no identity marker, so the client sends it
    assert cache_stem_is_relevant("session_guid", allowed, "us", foreign)
    assert cache_stem_is_relevant("tokens", allowed, "us", foreign)
    # Files keyed by profile name: only the active profile's file goes
    assert cache_stem_is_relevant("tokens_us", allowed, "us", foreign)
    assert not cache_stem_is_relevant("tokens_uk", allowed, "us", foreign)
    # Region prefix plus a matching digest (the region is not a profile name here)
    assert cache_stem_is_relevant(f"tokens_intl_{active.sha1}", allowed, "us", foreign)
    # An active-credential digest takes priority over a colliding foreign-profile token
    assert cache_stem_is_relevant(f"tokens_uk_{active.sha1}", allowed, "us", foreign)


def test_cache_stem_relevance_active_profile_beats_region_collision() -> None:
    from unshackle.core.remote_service import cache_stem_is_relevant

    # tokens_{profile}_{region}_{device}_{6hex} style key, where the region "us"
    # collides with a foreign profile named "us"
    assert cache_stem_is_relevant("tokens_default_us_androidtv_abc123", set(), "default", {"us", "de"})
    assert not cache_stem_is_relevant("tokens_us_us_androidtv_abc123", set(), "default", {"us", "de"})
    assert cache_stem_is_relevant("web_device_id_default_us", set(), "default", {"us"})


def test_cache_stem_relevance_without_credential_withholds_hex_stems() -> None:
    from unshackle.core.credential import Credential
    from unshackle.core.remote_service import cache_stem_is_relevant

    stray = Credential("someone@example.com", "pw")
    assert not cache_stem_is_relevant(f"tokens_{stray.sha1}", set(), "default", set())
    assert cache_stem_is_relevant("tokens_guest", set(), "default", set())


def test_cache_stem_relevance_separator_and_case_profile_names() -> None:
    from unshackle.core.remote_service import cache_stem_is_relevant

    # A foreign profile name with separators still matches
    assert not cache_stem_is_relevant("tokens_us-east", set(), "eu", {"us-east"})
    assert not cache_stem_is_relevant("tokens_us_east", set(), "eu", {"us-east"})
    # Case variants of a foreign profile name still match
    assert not cache_stem_is_relevant("tokens_UK", set(), "us", {"uk"})
    # The active profile matches through case and separator variants too
    assert cache_stem_is_relevant("tokens_US-EAST", set(), "us-east", {"eu"})
    # A longer matching foreign name beats an active name that is its prefix
    assert not cache_stem_is_relevant("tokens_us-east", set(), "us", {"us-east"})
    # ...but a shorter foreign match does not override the active name
    assert cache_stem_is_relevant("tokens_default_us_androidtv_abc123", set(), "default", {"us"})


def test_load_cache_files_wiring(tmp_path, monkeypatch) -> None:
    import json
    import logging
    from types import SimpleNamespace

    from unshackle.core.config import config
    from unshackle.core.credential import Credential
    from unshackle.core.remote_service import RemoteService

    tag = "TESTSVC"
    cache_dir = tmp_path / tag
    cache_dir.mkdir()
    other = Credential("other@example.com", "pw2")
    for stem in ("tokens_us", "tokens_uk", "tokens_default", "session_guid", f"tokens_{other.sha1}", "titles_abc"):
        (cache_dir / f"{stem}.json").write_text(json.dumps({"k": stem}))

    monkeypatch.setattr(config.directories, "cache", tmp_path)
    monkeypatch.setattr(
        config,
        "credentials",
        {tag: {"default": "a@example.com:pw", "us": "b@example.com:pw", "uk": "c@example.com:pw"}},
    )
    stub = SimpleNamespace(service_tag=tag, log=logging.getLogger("test"))

    sent = RemoteService.load_cache_files(stub, "us")
    assert set(sent) == {"tokens_us", "session_guid"}

    # A profile with no credentials entry falls back to the default credential
    sent = RemoteService.load_cache_files(stub, "missing")
    assert set(sent) == {"tokens_default", "session_guid"}


def test_resolve_manifest_data_matches_across_per_range_manifests() -> None:
    """One ISM manifest per range. The HDR10 track must take the quality level from its
    own manifest, not the same-sized one in the SDR manifest, in any manifest order."""
    import base64
    import zlib

    from tests.core.test_ism_init import VIDEO_HEVC_PQ_CPD, VIDEO_HEVC_SDR_CPD
    from tests.core.test_ism_range import manifest_xml
    from unshackle.core.manifests import ISM
    from unshackle.core.remote_service import resolve_manifest_data

    urls = {"sdr": "https://x/sdr/manifest", "hdr": "https://x/hdr/manifest"}
    xml = {"sdr": manifest_xml(VIDEO_HEVC_SDR_CPD), "hdr": manifest_xml(VIDEO_HEVC_PQ_CPD)}
    local = {k: ISM.from_text(xml[k], url=urls[k]).to_tracks(language="en").videos[0] for k in xml}
    assert local["sdr"].range == Video.Range.SDR and local["hdr"].range == Video.Range.HDR10

    remote_hdr = deserialize_video(
        {
            "id": str(local["hdr"].id),
            "codec": "HEVC",
            "width": 3840,
            "height": 2160,
            "language": "en",
            "range": "HDR10",
            "bitrate_bps": 15000000,
        }
    )
    tracks = build_tracks({"video": [], "audio": [], "subtitles": []})
    tracks.videos.append(remote_hdr)
    manifests = [
        {"type": "ism", "url": urls[k], "data": base64.b64encode(zlib.compress(xml[k].encode())).decode()}
        for k in ("sdr", "hdr")
    ]

    resolve_manifest_data(tracks, manifests)

    assert remote_hdr.data["ism"]["quality_level"].get("CodecPrivateData") == VIDEO_HEVC_PQ_CPD
