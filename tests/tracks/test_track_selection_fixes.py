"""Unit tests for the audio/video track-selection primitives.

Cover the pure ``Tracks`` helpers behind the ``dl`` selection layer:

- ``Tracks.by_resolutions``: the 16:9 canvas fallback must tolerate tracks with
  no known width instead of crashing on ``int(None * 9 / 16)``.
- ``Tracks.sort_videos``: resolution outranks bitrate, and an unknown-bitrate
  track can still win on resolution.
- ``Tracks.sort_audio``: descriptive-last, Atmos, codec_priority then bitrate;
  the first track after sorting is the "best" pick the dl layer selects.
"""

from __future__ import annotations

from unshackle.core.tracks import Audio, Tracks, Video


def make_video(
    track_id: str,
    *,
    height: int,
    bitrate: int | None,
    width: int | None = None,
    codec: Video.Codec = Video.Codec.HEVC,
) -> Video:
    return Video(
        id_=track_id,
        url=f"https://example.test/{track_id}.m3u8",
        language="en",
        codec=codec,
        range_=Video.Range.SDR,
        width=width,
        height=height,
        bitrate=bitrate,
    )


def make_audio(
    track_id: str,
    *,
    codec: Audio.Codec,
    bitrate: int,
    joc: int | None = None,
    descriptive: bool = False,
    language: str = "en",
) -> Audio:
    return Audio(
        id_=track_id,
        url=f"https://example.test/{track_id}.m4a",
        language=language,
        codec=codec,
        bitrate=bitrate,
        joc=joc,
        descriptive=descriptive,
    )


# by_resolutions: width=None must not crash the canvas fallback


def test_by_resolutions_tolerates_width_none_track() -> None:
    tracks = Tracks()
    tracks.videos = [
        make_video("h1080", height=1080, width=1920, bitrate=8_000_000),  # no exact/canvas 720 match
        make_video("no-width", height=480, width=None, bitrate=1_000_000),  # width-less, must be skipped
        make_video("canvas-720", height=406, width=1280, bitrate=3_000_000),  # 1280*9/16 == 720
    ]

    tracks.by_resolutions([720])

    assert [t.id for t in tracks.videos] == ["canvas-720"]


# sort_videos: resolution before bitrate


def test_sort_videos_resolution_outranks_bitrate() -> None:
    tracks = Tracks()
    tracks.videos = [
        make_video("hi-bitrate-720", height=720, bitrate=10_000_000, width=1280),
        make_video("lo-bitrate-2160", height=2160, bitrate=5_000_000, width=3840),
    ]

    tracks.sort_videos()

    assert [t.id for t in tracks.videos] == ["lo-bitrate-2160", "hi-bitrate-720"]


def test_sort_videos_unknown_bitrate_still_wins_on_resolution() -> None:
    tracks = Tracks()
    tracks.videos = [
        make_video("known-720", height=720, bitrate=10_000_000, width=1280),
        make_video("unknown-1080", height=1080, bitrate=None, width=1920),
    ]

    tracks.sort_videos()

    assert [t.id for t in tracks.videos] == ["unknown-1080", "known-720"]


# sort_audio: first track after sort is the "best" the dl layer picks


def test_sort_audio_atmos_ranks_first() -> None:
    tracks = Tracks()
    tracks.audio = [
        make_audio("ec3-320", codec=Audio.Codec.EC3, bitrate=320_000),
        make_audio("ac3-640", codec=Audio.Codec.AC3, bitrate=640_000),
        make_audio("ec3-atmos", codec=Audio.Codec.EC3, bitrate=768_000, joc=16),
        make_audio("ec3-desc", codec=Audio.Codec.EC3, bitrate=768_000, descriptive=True),
    ]

    tracks.sort_audio(codec_priority=["EC3", "AC3"])

    best = Tracks.by_language(tracks.audio, ["en"])[0]
    assert best.id == "ec3-atmos"
    # descriptive always sorts last
    assert tracks.audio[-1].id == "ec3-desc"


def test_sort_audio_codec_priority_beats_bitrate() -> None:
    tracks = Tracks()
    tracks.audio = [
        make_audio("ac3-640", codec=Audio.Codec.AC3, bitrate=640_000),
        make_audio("ec3-320", codec=Audio.Codec.EC3, bitrate=320_000),
    ]

    tracks.sort_audio(codec_priority=["EC3", "AC3"])

    best = Tracks.by_language(tracks.audio, ["en"])[0]
    # EC3 is prioritized even though the AC3 track has the higher bitrate
    assert best.id == "ec3-320"
