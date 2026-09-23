"""Tests for ``ImportService.get_tracks``, which rebuilds tracks from an export.

Regression: an export whose manifest labels its own streams imports even when the service
never set ``Title.language``. When nothing supplies a language the import fails naming the
service rather than blaming the manifest URL. A direct-URL subtitle anywhere in the export
replaces the manifest's whole subtitle set, since the service side-loaded it precisely
because the manifest's copy was the worse one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch
from uuid import UUID

import click
import pytest
import requests

from unshackle.core.import_service import ImportService
from unshackle.core.manifests import DASH
from unshackle.core.tracks import Audio, Subtitle, Video

MPD_TEMPLATE = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10M">
  <Period id="1">
    <AdaptationSet contentType="video" mimeType="video/mp4"{lang}>
      <Representation id="{video_id}" codecs="avc1.640028" bandwidth="5000000" width="1920" height="1080">
        <BaseURL>v.mp4</BaseURL><SegmentBase indexRange="0-1"/>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="text" mimeType="text/vtt"{lang}>
      <Representation id="{sub_id}" bandwidth="1000"><BaseURL>s.vtt</BaseURL><SegmentBase indexRange="0-1"/></Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

# labelled by @lang; unlabelled but with Unified Streaming ids DASH derives a language from;
# and labelled by neither, which is the only shape that needs the title's language
MPD_WITH_LANG = MPD_TEMPLATE.format(lang=' lang="ja"', video_id="v1", sub_id="s1")
MPD_ID_LANG = MPD_TEMPLATE.format(lang="", video_id="video_eng=5000000", sub_id="text_eng=1000")
MPD_NO_LANG = MPD_TEMPLATE.format(lang="", video_id="v1", sub_id="s1")

URL_SUBTITLE = {
    "type": "Subtitle",
    "id": "sideloaded-en",
    "url": "https://example.invalid/en.srt",
    "language": "en",
    "descriptor": "URL",
    "codec": "SubRip",
}


def make_service(tmp_path: Path, language: Optional[str], tracks: Optional[dict[str, Any]] = None) -> ImportService:
    export = {
        "version": 2,
        "service": "EXAMPLE",
        "titles": {
            "movie-1": {
                "meta": {"type": "movie", "id": "movie-1", "name": "Example Movie", "year": 2024, "language": language},
                "manifest_url": "https://example.invalid/m.mpd",
                "manifest_type": "DASH",
                "tracks": tracks or {},
            }
        },
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export), encoding="utf8")
    return ImportService(click.Context(click.Command("dl")), "EXAMPLE", "movie-1", str(path))


def get_tracks(service: ImportService, mpd: str):
    with patch.object(DASH, "from_url", side_effect=lambda url, session=None, **kw: DASH.from_text(mpd, url)):
        return service.get_tracks(next(iter(service.get_titles())))


@pytest.mark.parametrize("mpd", [MPD_WITH_LANG, MPD_ID_LANG], ids=["lang-attribute", "language-in-rep-id"])
def test_manifest_supplies_language_without_title_language(tmp_path: Path, mpd: str) -> None:
    """A manifest that labels its own streams imports fine with no language in the export."""
    tracks = get_tracks(make_service(tmp_path, language=None), mpd)
    assert [str(t.language) for t in tracks.videos] == ["ja" if mpd is MPD_WITH_LANG else "en"]


@pytest.mark.parametrize("language", [None, "und"], ids=["missing", "und"])
def test_unlabelled_manifest_without_usable_language_blames_the_service(
    tmp_path: Path, language: Optional[str]
) -> None:
    """`und` is truthy and a valid tag, so it must be rejected as firmly as no language at all."""
    with pytest.raises(click.ClickException) as error:
        get_tracks(make_service(tmp_path, language=language), MPD_NO_LANG)
    assert "Title.language" in error.value.message
    assert "may have expired" not in error.value.message


def test_exported_url_subtitles_replace_the_manifests(tmp_path: Path) -> None:
    """Services drop the manifest's subtitles and side-load their own; importing both duplicates them."""
    tracks = get_tracks(make_service(tmp_path, "ja", {"sideloaded-en": URL_SUBTITLE}), MPD_WITH_LANG)
    assert [(t.id, t.descriptor.name) for t in tracks.subtitles] == [("sideloaded-en", "URL")]
    assert len(tracks.videos) == 1


def test_direct_url_subtitles_win_over_every_manifest_one(tmp_path: Path) -> None:
    """One side-loaded subtitle condemns the whole re-parsed set, including other languages."""
    manifest_sub = {
        "type": "Subtitle",
        "id": "s1",
        "url": "https://example.invalid/m.mpd",
        "language": "ja",
        "descriptor": "DASH",
        "codec": "WebVTT",
    }
    tracks = get_tracks(
        make_service(tmp_path, "ja", {"s1": manifest_sub, "sideloaded-en": URL_SUBTITLE}), MPD_WITH_LANG
    )
    assert [(t.id, t.descriptor.name) for t in tracks.subtitles] == [("sideloaded-en", "URL")]


def test_unreadable_exported_track_is_skipped(tmp_path: Path) -> None:
    """A track dict this build cannot rebuild must not take the whole import down with it."""
    unknown = {**URL_SUBTITLE, "id": "future", "codec": "CODEC_FROM_A_LATER_BUILD"}
    tracks = get_tracks(make_service(tmp_path, "ja", {"sideloaded-en": URL_SUBTITLE, "future": unknown}), MPD_WITH_LANG)
    assert [t.id for t in tracks.subtitles] == ["sideloaded-en"]


@pytest.mark.parametrize(
    "failure",
    [requests.ConnectionError("dns"), TypeError("Expected 'MPD' document, but received a 'html' document instead.")],
    ids=["connection-error", "expired-token-html-page"],
)
def test_fetch_failure_still_reports_a_possible_expiry(tmp_path: Path, failure: Exception) -> None:
    """An expired signed URL can 200 with an error page, so document failures are expiries too."""
    service = make_service(tmp_path, "ja")
    title = next(iter(service.get_titles()))
    with patch.object(DASH, "from_url", side_effect=failure):
        with pytest.raises(click.ClickException) as error:
            service.get_tracks(title)
    assert "may have expired" in error.value.message


def test_a_subtitle_side_load_from_another_tool_is_added(tmp_path: Path) -> None:
    """A foreign export has no track dicts, so a ``tracks[]`` row with a url is the side-load."""
    export = {
        "kind": "mediaexport",
        "version": 1,
        "service": {"tag": "EXAMPLE"},
        "titles": [
            {
                "id": "movie-1",
                "kind": "clip",
                "title": "Example Movie",
                "language": "ja",
                "manifests": [{"url": "https://example.invalid/m.mpd", "type": "dash"}],
                "tracks": [
                    {"type": "video", "id": "v1", "codec": "avc"},
                    {
                        "type": "subtitle",
                        "id": "sub-en",
                        "url": "https://example.invalid/en.srt",
                        "codec": "srt",
                        "language": "en",
                        "sdh": True,
                    },
                    {"type": "subtitle", "id": "sub-x", "url": "https://example.invalid/x.srt"},
                ],
            }
        ],
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export), encoding="utf8")
    service = ImportService(click.Context(click.Command("dl")), "EXAMPLE", "movie-1", str(path))
    tracks = get_tracks(service, MPD_WITH_LANG)
    assert [(t.descriptor.name, str(t.language)) for t in tracks.subtitles] == [
        ("DASH", "ja"),
        ("URL", "en"),
        ("URL", "ja"),
    ]
    assert [t.id for t in tracks.subtitles[1:]] == ["sub-en", "sub-x"]
    assert tracks.subtitles[1].sdh and tracks.subtitles[1].codec is not None


VIDEO_KID, AUDIO_KID = "0a" * 16, "0b" * 16


def file_export(tmp_path: Path, rows: list[dict[str, Any]]) -> ImportService:
    """An export with no manifest: its ``tracks[]`` rows with a url are all of its media."""
    export = {
        "kind": "mediaexport",
        "version": 1,
        "service": {"tag": "EXAMPLE"},
        "titles": [
            {
                "id": "movie-1",
                "kind": "movie",
                "title": "Example Movie",
                "language": "fr",
                "drm": [{"system": "playready", "wrm_header": "<WRMHEADER/>"}],
                "keys": {VIDEO_KID: "a1" * 16, AUDIO_KID: "b2" * 16},
                "tracks": rows,
            }
        ],
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export), encoding="utf8")
    return ImportService(click.Context(click.Command("dl")), "EXAMPLE", "movie-1", str(path))


FILE_ROWS: list[dict[str, Any]] = [
    {
        "id": "v",
        "type": "video",
        "url": "https://example.invalid/v.mp4",
        "codec": "dvh1.05.07",
        "bitrate": 15000000,
        "width": 3840,
        "height": 2160,
        "fps": 25.0,
        "range": "dv",
        "headers": {"User-Agent": "exporter-ua"},
        "kids": [VIDEO_KID],
    },
    {
        "id": "a",
        "type": "audio",
        "url": "https://example.invalid/a.mp4",
        "codec": "ec-3",
        "language": "en",
        "bitrate": 578523,
        "channels": "6",
        "atmos": True,
        "kids": [AUDIO_KID],
    },
    {"id": "s", "type": "subtitle", "url": "https://example.invalid/s.mp4", "codec": "stpp", "language": "fr"},
]


def test_a_title_with_no_manifest_gets_a_track_for_each_file_row(tmp_path: Path) -> None:
    service = file_export(tmp_path, FILE_ROWS)
    tracks = service.get_tracks(next(iter(service.get_titles())))
    video, audio, subtitle = tracks.videos[0], tracks.audio[0], tracks.subtitles[0]
    assert [t.descriptor.name for t in (video, audio, subtitle)] == ["URL", "URL", "URL"]
    assert (video.codec, video.range, video.width, video.height, video.bitrate, str(video.language)) == (
        Video.Codec.HEVC,
        Video.Range.DV,
        3840,
        2160,
        15000000,
        "fr",
    )
    assert (audio.codec, audio.atmos, str(audio.language), audio.bitrate) == (Audio.Codec.EC3, True, "en", 578523)
    assert subtitle.codec == Subtitle.Codec.fTTML
    assert service.session.headers["User-Agent"] == "exporter-ua"


def test_each_file_track_gets_only_the_keys_for_its_kids(tmp_path: Path) -> None:
    service = file_export(tmp_path, FILE_ROWS)
    tracks = service.get_tracks(next(iter(service.get_titles())))
    assert tracks.videos[0].drm[0].content_keys == {UUID(hex=VIDEO_KID): "a1" * 16}
    assert tracks.audio[0].drm[0].content_keys == {UUID(hex=AUDIO_KID): "b2" * 16}
    assert not tracks.subtitles[0].drm


def test_a_file_row_can_name_its_codec_and_range_by_name(tmp_path: Path) -> None:
    row = {"id": "v", "type": "video", "url": "https://example.invalid/v.mp4", "codec": "hevc", "range": "hdr10p"}
    service = file_export(tmp_path, [row])
    video = service.get_tracks(next(iter(service.get_titles()))).videos[0]
    assert (video.codec, video.range) == (Video.Codec.HEVC, Video.Range.HDR10P)
