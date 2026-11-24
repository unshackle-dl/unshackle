"""An HLS export from another tool: only a master playlist URL, title keys, and no track rows.

The importer parses the master playlist for the tracks. Such a file can have no title
language, and its master playlist can name no key, so each track's own media playlist is
the only place that names its KIDs.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, cast
from unittest.mock import patch
from uuid import UUID

import click
import pytest

from unshackle.core.import_service import ImportService
from unshackle.core.manifests import HLS
from unshackle.core.tracks import Video

MASTER_URL = "https://example.invalid/hls/playlist.m3u8?t=1"
VIDEO_KID = UUID("00000000-499d-c8c7-6332-202020202020")
AUDIO_KID = UUID("00000000-499d-c8c7-6336-202020202020")
OTHER_KID = UUID("11111111-2222-3333-4444-555555555555")
KEYS = {VIDEO_KID: "0000000000000000000000000000000a", AUDIO_KID: "0000000000000000000000000000000b"}
KEYS[OTHER_KID] = "0000000000000000000000000000000c"


def playready_object(kid: UUID) -> str:
    """A base64 PlayReady object whose one WRM header names ``kid``."""
    value = base64.b64encode(kid.bytes_le).decode()
    xml = (
        '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="4.3.0.0"><DATA>'
        f'<PROTECTINFO><KIDS><KID ALGID="AESCBC" VALUE="{value}"></KID></KIDS></PROTECTINFO></DATA></WRMHEADER>'
    ).encode("utf-16-le")
    record = struct.pack("<HH", 1, len(xml)) + xml
    return base64.b64encode(struct.pack("<IH", len(record) + 6, 1) + record).decode()


def media_playlist(kid: Optional[UUID]) -> str:
    key = ""
    if kid:
        key = (
            '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.microsoft.playready",KEYFORMATVERSIONS="1",'
            f'URI="data:text/plain;charset=UTF-16;base64,{playready_object(kid)}"\n'
        )
    return (
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        f'#EXT-X-MAP:URI="init.mp4"\n{key}#EXTINF:6.0,\nseg0.m4s\n#EXT-X-ENDLIST\n'
    )


def master(audio_language: str = "en") -> str:
    lang = f'LANGUAGE="{audio_language}",' if audio_language else ""
    return (
        "#EXTM3U\n"
        f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",{lang}NAME="Main",DEFAULT=YES,URI="audio.m3u8"\n'
        '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="sub",LANGUAGE="en",NAME="English",URI="sub.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.640028,ec-3",'
        'AUDIO="aud",SUBTITLES="sub"\nvideo.m3u8\n'
    )


def write_export(tmp_path: Path, language: str = "", manifest_url: str = MASTER_URL) -> Path:
    """An export in the other tool's own format, as that tool writes it."""
    title: dict[str, Any] = {"id": "ep-1", "kind": "episode", "name": "Example Show", "season": 1, "episode": 1}
    if language:
        title["language"] = language
    doc = {
        "kind": "unidl-export",
        "version": 1,
        "service": "example",
        "titles": [
            {
                "save_name": "Example.Show.S01E01",
                "title": title,
                "keys": [f"{kid.hex}:{key}" for kid, key in KEYS.items()],
                "manifest_url": manifest_url,
                "headers": {"User-Agent": "Example/1.0"},
                "drm": {"system": "playready", "wrm_header": "<WRMHEADER/>"},
            }
        ],
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(doc), encoding="utf8")
    return path


def import_title(export: Path, master_text: str, playlists: dict[str, Optional[UUID]]) -> tuple[ImportService, Any]:
    ctx = cast(click.Context, SimpleNamespace(parent=None, params={}))
    svc = ImportService(ctx, "EXAMPLE", "ep-1", str(export))

    def get(url: str, **_: Any) -> SimpleNamespace:
        name = url.split("?")[0].rsplit("/", 1)[-1]
        return SimpleNamespace(text=media_playlist(playlists.get(name)), raise_for_status=lambda: None)

    setattr(svc.session, "get", get)
    episode = next(iter(svc.get_titles()))
    with patch.object(HLS, "from_url", side_effect=lambda url, session=None, **kw: HLS.from_text(master_text, url)):
        episode.tracks = svc.get_tracks(episode)
    return svc, episode


def test_no_title_language_takes_the_default_audio_language(tmp_path: Path) -> None:
    _, episode = import_title(write_export(tmp_path), master(), {})

    assert [str(t.language) for t in episode.tracks.videos] == ["en"]


def test_no_language_anywhere_names_the_missing_language(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException, match="No language"):
        import_title(write_export(tmp_path), master(audio_language=""), {})


def test_each_hls_track_gets_the_keys_its_media_playlist_names(tmp_path: Path) -> None:
    """The master playlist names no key, so only the media playlist tells the tracks apart."""
    svc, episode = import_title(
        write_export(tmp_path, "en"), master(), {"video.m3u8": VIDEO_KID, "audio.m3u8": AUDIO_KID}
    )
    svc.resolve_server_keys(episode)

    video, audio = episode.tracks.videos[0], episode.tracks.audio[0]
    assert video.drm and video.drm[0].content_keys == {VIDEO_KID: KEYS[VIDEO_KID]}
    assert audio.drm and audio.drm[0].content_keys == {AUDIO_KID: KEYS[AUDIO_KID]}
    assert all(not t.drm for t in episode.tracks.subtitles)


def test_an_hls_track_whose_playlist_names_no_key_stays_clear(tmp_path: Path) -> None:
    svc, episode = import_title(write_export(tmp_path, "en"), master(), {"video.m3u8": VIDEO_KID})
    svc.resolve_server_keys(episode)

    assert not episode.tracks.audio[0].drm
    assert isinstance(episode.tracks.videos[0], Video) and episode.tracks.videos[0].drm


def test_a_manifest_that_unshackle_cannot_read_stops_the_import(tmp_path: Path) -> None:
    """A manifest held only in the other tool's own form must not import as a title with no tracks."""
    with pytest.raises(click.ClickException, match="cannot read"):
        import_title(write_export(tmp_path, "en", manifest_url="x-unidl:json_manifest"), master(), {})
