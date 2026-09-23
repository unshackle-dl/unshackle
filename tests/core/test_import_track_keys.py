"""An import gives each encrypted track only the exported keys for the KIDs it declares.

A decrypter gives the all-zero KID the track's own content key only when it can tell which
key that is. A track that holds the whole title pool makes that impossible, so the pool is
the fallback for a track that declares no KID.
"""

from __future__ import annotations

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
from unshackle.core.manifests import DASH
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


def write_export(tmp_path: Path) -> Path:
    """Export the ladder as a finished download would: one licensed DRM per track."""
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
    return export


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


def test_each_track_gets_only_the_keys_for_its_own_kids(tmp_path: Path) -> None:
    keys = keys_by_track(import_service(write_export(tmp_path)))

    assert keys["video"] == {VIDEO_KID: VIDEO_KEY}
    assert keys["en"] == {AUDIO_KID: AUDIO_KEY}


def test_a_track_that_declares_no_kid_gets_the_whole_pool(tmp_path: Path) -> None:
    keys = keys_by_track(import_service(write_export(tmp_path)))

    assert keys["de"] == POOL


def test_a_rebuilt_drm_holds_only_the_keys_for_the_kids_its_pssh_names(tmp_path: Path) -> None:
    svc = import_service(write_export(tmp_path))
    single: list[dict[str, Any]] = [widevine(VIDEO_KID, VIDEO_KEY).to_dict()]

    with patch.object(svc, "title_drm_dicts", return_value=single):
        rebuilt = svc.rebuild_drm({"id": "u1"}, "movie-1")

    assert rebuilt and rebuilt[0].content_keys == {VIDEO_KID: VIDEO_KEY}


def test_a_title_with_several_drm_entries_gives_no_track_a_guessed_own_key(tmp_path: Path) -> None:
    """Which entry belongs to a direct-URL track is unknown, so the track gets a stub over the
    pool, and the zero-KID fallback stays empty instead of taking the first entry's key."""
    svc = import_service(write_export(tmp_path))

    rebuilt = svc.rebuild_drm({"id": "u1"}, "movie-1")

    assert rebuilt
    drm = rebuilt[0]
    assert drm.content_keys == POOL
    assert not [arg for arg in drm.mp4decrypt_key_args() if arg.startswith(ZERO_KID)]
