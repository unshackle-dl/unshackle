from __future__ import annotations

import logging

import pytest

from unshackle.core.tracks import Audio
from unshackle.core.wire import build_tracks, serialize_audio_track, served_url


def audio(url: str = "") -> Audio:
    return Audio(url=url, id_="a1", language="en", codec=Audio.Codec.AAC, bitrate=24000, channels=2.0)


def counted(calls: list[int]) -> str:
    calls.append(1)
    return "https://cdn.example/a"


def test_ensure_url_resolves_once() -> None:
    track, calls = audio(), list[int]()
    track.resolve_url = lambda: counted(calls)
    track.ensure_url()
    track.ensure_url()
    assert track.url == "https://cdn.example/a"
    assert calls == [1]


def test_ensure_url_keeps_a_given_url() -> None:
    track = audio("https://cdn.example/given")
    track.resolve_url = lambda: pytest.fail("a track with a URL must not resolve one")
    track.ensure_url()
    assert track.url == "https://cdn.example/given"


def test_a_track_with_no_url_and_no_resolver_stays_empty() -> None:
    track = audio()
    track.ensure_url()
    assert track.url == ""
    assert served_url(track) is None


def test_the_track_list_marks_a_track_that_resolves_its_url_on_use() -> None:
    track, calls = audio(), list[int]()
    track.resolve_url = lambda: counted(calls)
    sent = serialize_audio_track(track, include_url=True)
    assert sent["resolve_url"] is True and "url" not in sent
    assert calls == []
    assert serialize_audio_track(audio("https://cdn.example/b"), include_url=True)["url"] == "https://cdn.example/b"


def test_a_marked_track_arrives_with_no_url_and_others_keep_theirs() -> None:
    tracks = build_tracks(
        {
            "audio": [
                {"id": "a1", "codec": "AAC", "resolve_url": True},
                {"id": "a2", "codec": "AAC", "url": "https://x/2"},
            ]
        }
    )
    assert [track.url for track in tracks.audio] == ["", "https://x/2"]


def test_the_segments_route_resolves_the_url() -> None:
    track, calls = audio(), list[int]()
    track.resolve_url = lambda: counted(calls)
    assert served_url(track) == "https://cdn.example/a"
    assert calls == [1]


def test_a_failed_resolve_gives_no_url(caplog: pytest.LogCaptureFixture) -> None:
    track = audio()

    def refuse() -> str:
        raise RuntimeError("HTTP 404")

    track.resolve_url = refuse
    with caplog.at_level(logging.WARNING, logger="api"):
        assert served_url(track) is None
    assert "Could not resolve the URL of track a1: HTTP 404" in caplog.text
