"""An import gives each encrypted track only the exported keys for the KIDs it declares.

A decrypter gives the all-zero KID the track's own content key only when it can tell which
key that is. A track that holds the whole title pool makes that impossible, so the pool is
the fallback for a track that declares no KID.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID

import click
from pywidevine.pssh import PSSH

from unshackle.commands.dl import dl
from unshackle.core.drm import Widevine
from unshackle.core.drm.key_args import ZERO_KID
from unshackle.core.import_service import ImportService
from unshackle.core.manifests import DASH, HLS
from unshackle.core.titles import Movie
from unshackle.core.tracks import Video

MPD_URL = "https://example.invalid/m.mpd"
VIDEO_KID = UUID("00112233-4455-6677-8899-aabbccddeeff")
AUDIO_KID = UUID("ffeeddcc-bbaa-9988-7766-554433221100")
VIDEO_KEY = "0000000000000000000000000000000a"
AUDIO_KEY = "0000000000000000000000000000000b"
POOL = {VIDEO_KID: VIDEO_KEY, AUDIO_KID: AUDIO_KEY}


def protection(kid: UUID | None) -> str:
    default_kid = f' cenc:default_KID="{kid}"' if kid else ""
    return f'<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc"{default_kid}/>'


MPD = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2013" type="static"
     mediaPresentationDuration="PT10M">
  <Period id="1">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      {protection(VIDEO_KID)}
      <Representation id="v1" codecs="avc1.640028" bandwidth="5000000" width="1920" height="1080">
        <BaseURL>v.mp4</BaseURL><SegmentBase indexRange="0-1"/>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="audio" mimeType="audio/mp4" lang="en">
      {protection(AUDIO_KID)}
      <Representation id="a1" codecs="mp4a.40.2" bandwidth="128000">
        <BaseURL>a.mp4</BaseURL><SegmentBase indexRange="0-1"/>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="audio" mimeType="audio/mp4" lang="de">
      {protection(None)}
      <Representation id="a2" codecs="mp4a.40.2" bandwidth="128000">
        <BaseURL>a2.mp4</BaseURL><SegmentBase indexRange="0-1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


class StubService:
    """Stands in for the service class slot on Movie; never instantiated."""


def widevine(kid: UUID, key: str) -> Widevine:
    drm = Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[kid]), kid=kid)
    drm.content_keys[kid] = key
    return drm


def write_export(tmp_path: Path, kids: bool = True) -> Path:
    """Export the ladder as a finished download would: one licensed DRM per track.

    ``kids=False`` removes the per-track KID lists, as in a file from before they existed.
    """
    title = Movie(id_="movie-1", service=StubService, name="Example Movie", year=2024, language="en")
    for track in DASH.from_text(MPD, MPD_URL).to_tracks(language="en"):
        title.tracks.add(track)
    title.tracks.manifest_url = MPD_URL
    runner = dl.__new__(dl)
    runner.service = "EXAMPLE"
    runner.log = logging.getLogger("download")
    export = tmp_path / "export.json"
    drm = {"video": widevine(VIDEO_KID, VIDEO_KEY), "en": widevine(AUDIO_KID, AUDIO_KEY)}
    for track in [*title.tracks.videos, *title.tracks.audio]:
        runner.write_export(export, title, track, drm.get(label(track)))
    if not kids:
        doc = json.loads(export.read_text(encoding="utf8"))
        for row in doc["titles"][0]["tracks"]:
            row.pop("kids", None)
        export.write_text(json.dumps(doc), encoding="utf8")
    return export


def rows(export: Path) -> dict[str, dict[str, Any]]:
    """The export's track rows, by the same labels as the tracks."""
    doc = json.loads(export.read_text(encoding="utf8"))
    return {"video" if r["type"] == "video" else r["language"]: r for r in doc["titles"][0]["tracks"]}


def label(track: Any) -> str:
    """The video track, or an audio track by its language: the manifest gives no stable ids."""
    return "video" if isinstance(track, Video) else str(track.language)


def import_service(export: Path) -> ImportService:
    ctx = cast(click.Context, SimpleNamespace(parent=None, params={}))
    return ImportService(ctx, "EXAMPLE", "movie-1", str(export))


def keys_by_track(svc: ImportService) -> dict[str, dict[UUID, str]]:
    movie = next(iter(svc.get_titles()))
    with patch.object(DASH, "from_url", side_effect=lambda url, session=None, **kw: DASH.from_text(MPD, url)):
        movie.tracks = svc.get_tracks(movie)
    svc.resolve_server_keys(movie)
    return {label(t): dict(t.drm[0].content_keys) for t in [*movie.tracks.videos, *movie.tracks.audio]}


def test_the_export_lists_each_tracks_kids(tmp_path: Path) -> None:
    exported = rows(write_export(tmp_path))

    assert exported["video"]["kids"] == [VIDEO_KID.hex]
    assert exported["en"]["kids"] == [AUDIO_KID.hex]
    assert "kids" not in exported["de"], "a track exported without DRM names no KID"


def test_listed_kids_give_each_track_its_keys_without_a_probe(tmp_path: Path) -> None:
    svc = import_service(write_export(tmp_path))
    probed: list[str] = []
    track_kids = svc.track_kids

    def spy(track: Any, session: Any = None) -> Any:
        probed.append(label(track))
        return track_kids(track, session)

    with patch.object(svc, "track_kids", side_effect=spy):
        keys = keys_by_track(svc)

    assert keys["video"] == {VIDEO_KID: VIDEO_KEY}
    assert keys["en"] == {AUDIO_KID: AUDIO_KEY}
    assert probed == ["de"]


def test_a_listed_kid_picks_the_drm_entry_of_a_direct_url_track(tmp_path: Path) -> None:
    export = write_export(tmp_path)
    svc = import_service(export)

    rebuilt = svc.rebuild_drm({"id": rows(export)["en"]["id"]}, "movie-1")

    assert rebuilt and isinstance(rebuilt[0], Widevine)
    assert rebuilt[0].kids == [AUDIO_KID]
    assert rebuilt[0].content_keys == {AUDIO_KID: AUDIO_KEY}


def test_each_track_gets_only_the_keys_for_its_own_kids(tmp_path: Path) -> None:
    """An export from before the per-track KID lists falls back to the KIDs the manifest declares."""
    keys = keys_by_track(import_service(write_export(tmp_path, kids=False)))

    assert keys["video"] == {VIDEO_KID: VIDEO_KEY}
    assert keys["en"] == {AUDIO_KID: AUDIO_KEY}


def test_a_track_that_declares_no_kid_gets_the_whole_pool(tmp_path: Path) -> None:
    keys = keys_by_track(import_service(write_export(tmp_path, kids=False)))

    assert keys["de"] == POOL


def test_a_rebuilt_drm_holds_only_the_keys_for_the_kids_its_pssh_names(tmp_path: Path) -> None:
    svc = import_service(write_export(tmp_path, kids=False))
    single: list[dict[str, Any]] = [widevine(VIDEO_KID, VIDEO_KEY).to_dict()]

    with patch.object(svc, "title_drm_dicts", return_value=single):
        rebuilt = svc.rebuild_drm({"id": "u1", "type": "Video"}, "movie-1")

    assert rebuilt and rebuilt[0].content_keys == {VIDEO_KID: VIDEO_KEY}


def test_a_title_with_several_drm_entries_gives_no_track_a_guessed_own_key(tmp_path: Path) -> None:
    """Which entry belongs to a direct-URL track is unknown, so the track gets a stub over the
    pool, and the zero-KID fallback stays empty instead of taking the first entry's key."""
    svc = import_service(write_export(tmp_path, kids=False))

    rebuilt = svc.rebuild_drm({"id": "u1", "type": "Video"}, "movie-1")

    assert rebuilt
    drm = rebuilt[0]
    assert drm.content_keys == POOL
    assert not [arg for arg in drm.mp4decrypt_key_args() if arg.startswith(ZERO_KID)]


HLS_URL = "https://example.invalid/master.m3u8"
HLS_MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",LANGUAGE="en",NAME="English",DEFAULT=YES,URI="audio_en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="sub",LANGUAGE="es",NAME="Spanish",URI="sub_es.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="aud",SUBTITLES="sub"
1080.m3u8
"""
SUB_KID = UUID("0123456789abcdef0123456789abcdef")
SUB_KEY = "0000000000000000000000000000000c"


def hls_import(
    tmp_path: Path, audio_drm: Widevine | None = None, subtitle_drm: Widevine | None = None, kids: bool = True
) -> dict[str, Any]:
    """Export an HLS ladder with a licensed video track, import it, and return each track's DRM.

    HLS tracks come back from their exported dicts, and HLS.download_track decrypts every
    track that holds a DRM.
    """
    title = Movie(id_="movie-1", service=StubService, name="Example Movie", year=2024, language="en")
    for track in HLS.from_text(HLS_MASTER, HLS_URL).to_tracks(language="en"):
        title.tracks.add(track)
    title.tracks.manifest_url = HLS_URL
    runner = dl.__new__(dl)
    runner.service = "EXAMPLE"
    runner.log = logging.getLogger("download")
    export = tmp_path / "export.json"
    drm = {"video": widevine(VIDEO_KID, VIDEO_KEY), "en": audio_drm, "es": subtitle_drm}
    for track in [*title.tracks.videos, *title.tracks.audio, *title.tracks.subtitles]:
        runner.write_export(export, title, track, drm.get(label(track)))
    if not kids:
        doc = json.loads(export.read_text(encoding="utf8"))
        for row in doc["titles"][0]["tracks"]:
            row.pop("kids", None)
        export.write_text(json.dumps(doc), encoding="utf8")
    svc = import_service(export)
    movie = next(iter(svc.get_titles()))
    movie.tracks = svc.get_tracks(movie)
    svc.resolve_server_keys(movie)
    return {label(t): t.drm for t in movie.tracks}


def test_a_clear_subtitle_gets_no_drm_from_the_key_pool(tmp_path: Path) -> None:
    """An export from before the per-track KID lists cannot tell a clear video or audio track
    from an encrypted one, so those keep the pool. A subtitle is clear."""
    drm = hls_import(tmp_path, audio_drm=widevine(AUDIO_KID, AUDIO_KEY), kids=False)

    assert drm["es"] is None, "a clear subtitle that holds a DRM goes to a decrypter that empties it"
    assert drm["video"] and drm["video"][0].content_keys == POOL, "a video that declares no KID keeps the pool"


def test_a_keyed_video_leaves_a_clear_audio_track_clear(tmp_path: Path) -> None:
    drm = hls_import(tmp_path)

    assert drm["video"] and drm["video"][0].content_keys == {VIDEO_KID: VIDEO_KEY}
    assert drm["en"] is None, "the export lists KIDs for its encrypted tracks, so an audio track with none is clear"
    assert drm["es"] is None


def test_a_subtitle_that_lists_its_kid_keeps_its_key(tmp_path: Path) -> None:
    drm = hls_import(tmp_path, subtitle_drm=widevine(SUB_KID, SUB_KEY))

    assert drm["es"] and drm["es"][0].content_keys == {SUB_KID: SUB_KEY}
