import pytest

from unshackle.core import binaries
from unshackle.core.tracks import Tracks, Video


def make_video(track_id: str, **data) -> Video:
    video = Video(
        id_=track_id,
        url=f"https://example.test/{track_id}.m3u8",
        language="zh",
        codec=Video.Codec.AVC,
        range_=Video.Range.SDR,
        width=1920,
        height=1080,
        bitrate=5_000_000,
    )
    video.data.update(data)
    return video


def mux_until_mkvmerge_check(tracks: Tracks) -> None:
    # the embedded-audio metadata pass runs before the mkvmerge lookup, so a missing
    # binary stops the mux right after the state under test has been written
    with pytest.raises(RuntimeError, match="MKVToolNix"):
        tracks.mux("Test Title")


def test_names_undeclared_video(monkeypatch):
    monkeypatch.setattr(binaries, "MKVToolNix", None)
    video = make_video("v1")
    tracks = Tracks(video)

    mux_until_mkvmerge_check(tracks)

    assert video.needs_repack
    assert video.data["audio_language"] == "zh"
    assert video.data["audio_language_name"] == "Chinese"


def test_service_declaration_is_preserved(monkeypatch):
    monkeypatch.setattr(binaries, "MKVToolNix", None)
    declared = make_video("v1", audio_language="th", audio_language_name="Thai [Original]")
    undeclared = make_video("v2")
    tracks = Tracks([declared, undeclared])

    mux_until_mkvmerge_check(tracks)

    assert declared.data["audio_language"] == "th"
    assert declared.data["audio_language_name"] == "Thai [Original]"
    assert not declared.needs_repack
    assert undeclared.data["audio_language"] == "zh"
