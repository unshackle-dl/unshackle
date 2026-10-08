"""Regression test for a licence-only HLS run on a playlist that changes its key part-way.

``HLS.download_track`` licensed only the first key before it returned, so an export made
with no download held no content key for the later KID, and the import failed there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from pywidevine.pssh import PSSH

from unshackle.core.constants import DOWNLOAD_LICENCE_ONLY
from unshackle.core.manifests.hls import HLS
from unshackle.core.remote_service import segment_uri_filter
from unshackle.core.tracks import Video
from unshackle.core.tracks.track import DownloadContext, Track

FIRST_KID = UUID("11111111-1111-1111-1111-111111111111")
LATER_KID = UUID("22222222-2222-2222-2222-222222222222")


def key_tag(kid: UUID) -> str:
    pssh = PSSH.new(key_ids=[kid], system_id=PSSH.SystemId.Widevine).dumps()
    return (
        '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="data:text/plain;base64,'
        f'{pssh}",KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",KEYFORMATVERSIONS="1"'
    )


PLAYLIST = f"""#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:6
{key_tag(FIRST_KID)}
#EXTINF:6.0,
seg0.m4s
#EXT-X-DISCONTINUITY
{key_tag(LATER_KID)}
#EXTINF:6.0,
seg1.m4s
#EXT-X-ENDLIST
"""


def test_licence_only_licenses_a_key_that_starts_part_way(tmp_path: Path, monkeypatch: Any) -> None:
    playlist = tmp_path / "media.m3u8"
    playlist.write_text(PLAYLIST, encoding="utf-8")
    track = Video(
        url="https://example.test/media.m3u8",
        language="en",
        descriptor=Track.Descriptor.HLS,
        codec=Video.Codec.HEVC,
    )
    track.from_file = playlist
    # the KID normally comes from the init segment; this test is offline
    monkeypatch.setattr(HLS, "get_track_kid_from_init", staticmethod(lambda *args, **kwargs: None))

    licensed: list[UUID] = []
    ctx = DownloadContext(
        save_path=tmp_path / "video.mp4",
        save_dir=tmp_path / "video_segments",
        progress=lambda **_: None,
        license_widevine=lambda drm, track_kid=None: licensed.append(track_kid),
        cdm=None,
    )

    DOWNLOAD_LICENCE_ONLY.set()
    try:
        HLS.download_track(track=track, ctx=ctx)
    finally:
        DOWNLOAD_LICENCE_ONLY.clear()

    assert licensed == [FIRST_KID, LATER_KID]
    assert track.data["hls"]["unwanted_segments"] == []


def test_licence_only_skips_the_key_of_a_filtered_segment(tmp_path: Path, monkeypatch: Any) -> None:
    """The filtered segment URIs go to the track data, and an import filter built from them drops the same segment."""
    playlist = tmp_path / "media.m3u8"
    playlist.write_text(PLAYLIST, encoding="utf-8")
    monkeypatch.setattr(HLS, "get_track_kid_from_init", staticmethod(lambda *args, **kwargs: None))

    def licence_only(segment_filter: Any) -> tuple[list[UUID], list[str]]:
        track = Video(
            url="https://example.test/media.m3u8",
            language="en",
            descriptor=Track.Descriptor.HLS,
            codec=Video.Codec.HEVC,
        )
        track.from_file = playlist
        track.OnSegmentFilter = segment_filter
        licensed: list[UUID] = []
        ctx = DownloadContext(
            save_path=tmp_path / "video.mp4",
            save_dir=tmp_path / "video_segments",
            progress=lambda **_: None,
            license_widevine=lambda drm, track_kid=None: licensed.append(track_kid),
            cdm=None,
        )
        DOWNLOAD_LICENCE_ONLY.set()
        try:
            HLS.download_track(track=track, ctx=ctx)
        finally:
            DOWNLOAD_LICENCE_ONLY.clear()
        return licensed, track.data["hls"]["unwanted_segments"]

    licensed, unwanted = licence_only(lambda segment: segment.uri == "seg1.m4s")
    assert licensed == [FIRST_KID]
    assert len(unwanted) == 1 and unwanted[0].endswith("seg1.m4s")

    assert licence_only(segment_uri_filter(unwanted)) == (licensed, unwanted)


def test_licence_only_warns_when_no_dropped_segment_is_in_the_playlist(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """A changed host or path makes an exported list match nothing, and the download then keeps every segment."""
    playlist = tmp_path / "media.m3u8"
    playlist.write_text(PLAYLIST, encoding="utf-8")
    monkeypatch.setattr(HLS, "get_track_kid_from_init", staticmethod(lambda *args, **kwargs: None))
    track = Video(
        url="https://example.test/media.m3u8",
        language="en",
        descriptor=Track.Descriptor.HLS,
        codec=Video.Codec.HEVC,
    )
    track.from_file = playlist
    stale = ["https://other.example/moved.m4s"]
    track.OnSegmentFilter = segment_uri_filter(stale)
    track.data["hls"]["unwanted_segments"] = stale
    ctx = DownloadContext(
        save_path=tmp_path / "video.mp4",
        save_dir=tmp_path / "video_segments",
        progress=lambda **_: None,
        license_widevine=lambda drm, track_kid=None: None,
        cdm=None,
    )

    DOWNLOAD_LICENCE_ONLY.set()
    try:
        with caplog.at_level("WARNING", logger="HLS"):
            HLS.download_track(track=track, ctx=ctx)
    finally:
        DOWNLOAD_LICENCE_ONLY.clear()

    assert "keeps every segment" in caplog.text


def test_licence_only_fetches_no_aes_128_key(tmp_path: Path, monkeypatch: Any) -> None:
    """An AES-128 key with an IV on each segment is a new key each time, and licence-only must not fetch them."""
    playlist = tmp_path / "media.m3u8"
    playlist.write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
        + "".join(
            f'#EXT-X-KEY:METHOD=AES-128,URI="https://example.test/key",IV=0x{i:032x}\n#EXTINF:6.0,\nseg{i}.ts\n'
            for i in range(1, 4)
        )
        + "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    track = Video(
        url="https://example.test/media.m3u8",
        language="en",
        descriptor=Track.Descriptor.HLS,
        codec=Video.Codec.AVC,
    )
    track.from_file = playlist
    fetched: list[Any] = []
    monkeypatch.setattr(HLS, "get_drm", staticmethod(lambda key, session=None: fetched.append(key)))
    ctx = DownloadContext(
        save_path=tmp_path / "video.mp4",
        save_dir=tmp_path / "video_segments",
        progress=lambda **_: None,
        license_widevine=lambda drm, track_kid=None: None,
        cdm=None,
    )

    DOWNLOAD_LICENCE_ONLY.set()
    try:
        HLS.download_track(track=track, ctx=ctx)
    finally:
        DOWNLOAD_LICENCE_ONLY.clear()

    assert len(fetched) <= 1, "only the first key of the playlist, as before"
