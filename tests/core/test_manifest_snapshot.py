"""A manifest snapshot must keep the periods a service filtered out across a capture, a JSON trip and a restore."""

from __future__ import annotations

import base64
import json
import logging
import zlib
from copy import deepcopy
from typing import Any, Optional

import pytest

from unshackle.core.manifests import DASH
from unshackle.core.manifests import snapshot as snapshot_module
from unshackle.core.manifests.snapshot import capture, parse, restore
from unshackle.core.tracks import Tracks
from unshackle.core.tracks.track import Track

URL = "https://cdn.example/title/manifest.mpd"


def period(pid: Optional[str], kind: str) -> str:
    attr = f' id="{pid}"' if pid else ""
    name = pid or "noid"
    return f"""
  <Period{attr} duration="PT30S">
    <SupplementalProperty schemeIdUri="urn:example:adtype" value="{kind}"/>
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="v1" codecs="avc1.640028" bandwidth="5000000" width="1920" height="1080" frameRate="24">
        <SegmentTemplate media="{name}/v1_$Number$.m4s" initialization="{name}/v1_init.mp4"
                         duration="4" timescale="1" startNumber="1"/>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="audio" mimeType="audio/mp4" lang="en">
      <Representation id="a1" codecs="mp4a.40.2" bandwidth="128000" audioSamplingRate="48000">
        <SegmentTemplate media="{name}/a1_$Number$.m4s" initialization="{name}/a1_init.mp4"
                         duration="4" timescale="1" startNumber="1"/>
      </Representation>
    </AdaptationSet>
  </Period>"""


def mpd(*periods: str) -> str:
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT3M" minBufferTime="PT2S"
     profiles="urn:mpeg:dash:profile:isoff-live:2011">{"".join(periods)}
</MPD>"""


MPD = mpd(
    period("pre-1", "PRE_ROLL"),
    period("main-1", "CONTENT"),
    period(None, "MID_ROLL"),
    period("main-2", "CONTENT"),
    period("mid-2", "MID_ROLL"),
    period("main-3", "CONTENT"),
)


def is_ad(p: Any) -> bool:
    return any(x.get("value", "").endswith("_ROLL") for x in p.findall("SupplementalProperty"))


def service_tracks(text: str = MPD) -> Tracks:
    return DASH.from_text(text, URL).to_tracks(language="en", period_filter=is_ad)


def detach_video(tracks: Tracks, track_id: Optional[str] = None) -> Track:
    """Give the video an AdaptationSet outside the tree, the way a service that builds its own does."""
    video = tracks.videos[0]
    data = video.data["dash"]
    data["adaptation_set"] = deepcopy(data["adaptation_set"])
    data["representation"] = data["adaptation_set"].find("Representation")
    if track_id:
        video.id = track_id
    return video


def export_and_import(tmp_path: Any, tracks: Tracks) -> Tracks:
    from tests.core.test_export import StubService, import_ctx, make_dl
    from unshackle.core.import_service import ImportService
    from unshackle.core.titles import Movie

    export = tmp_path / "export.json"
    title = Movie(id_="movie-1", service=StubService, name="Example Movie", year=2024, language="en")
    title.tracks = tracks
    runner = make_dl()
    for track in title.tracks:
        runner.write_export(export, title, track)
    service = ImportService(import_ctx(), "EXAMPLE", "movie-1", str(export))
    return service.get_tracks(next(iter(service.get_titles())))


def stitched(track: Track) -> list[str]:
    """The media template of each period download_track would stitch for this track."""
    data = track.data["dash"]
    filtered = data.get("filtered_period_ids", [])
    out = []
    for p in data["manifest"].findall("Period"):
        if not DASH.is_content_period(p, filtered):
            continue
        if p is data["period"]:
            rep = data["representation"]
        else:
            found = DASH.resolve_representation(p, data["representation_id"], data["adaptation_set"])
            if found is None:
                continue
            rep = found[1]
        out.append(rep.find("SegmentTemplate").get("media").split("/")[0])
    return out


def round_trip(tracks: Tracks, level: int = -1) -> Tracks:
    snapshot = json.loads(json.dumps(capture(tracks, level)))
    assert {"manifests", "track_manifests"} <= set(snapshot)
    client = Tracks()
    for track in tracks:
        client.add(Track.from_dict(track.to_dict()))
    restore(client, snapshot)
    return client


def inflated_size(entry: dict[str, Any]) -> int:
    return len(zlib.decompress(base64.b64decode(entry["data"])))


def test_service_filter_drops_every_ad_period_after_the_first_content_period():
    assert stitched(service_tracks().videos[0]) == ["main-1", "main-2", "main-3"]


def test_whole_manifest_restore_keeps_the_service_filter():
    video = round_trip(service_tracks()).videos[0]
    assert video.data["dash"]["period"].get("id") == "main-1"
    assert stitched(video) == ["main-1", "main-2", "main-3"]


def test_one_track_fragment_keeps_every_period_and_the_service_filter():
    tracks = service_tracks()
    detach_video(tracks)
    snapshot = json.loads(json.dumps(capture(tracks)))
    assert len(snapshot["track_manifests"]) == 1
    video = round_trip(tracks).videos[0]
    assert stitched(video) == ["main-1", "main-2", "main-3"]


def test_import_restores_the_service_filter_from_the_export_without_a_fetch(tmp_path):
    video = export_and_import(tmp_path, service_tracks()).videos[0]
    assert stitched(video) == ["main-1", "main-2", "main-3"]


def test_capture_on_a_remote_client_saves_the_whole_manifest_not_a_fragment():
    tracks = service_tracks()
    detach_video(tracks)
    client = round_trip(tracks)
    saved = parse(capture(client), URL, "en")
    assert saved is not None
    assert (len(saved.videos), len(saved.audio)) == (1, 1)


def test_import_rebuilds_a_service_built_track_from_its_fragment(tmp_path):
    tracks = service_tracks()
    detach_video(tracks, "service-built")
    imported = export_and_import(tmp_path, tracks)
    video = next(t for t in imported.videos if t.id == "service-built")
    assert stitched(video) == ["main-1", "main-2", "main-3"]


def test_a_filtered_period_that_reuses_a_kept_id_keeps_the_content():
    tracks = service_tracks(mpd(period("main-1", "CONTENT"), period("main-1", "MID_ROLL"), period("main-2", "CONTENT")))
    assert stitched(tracks.videos[0]) == ["main-1", "main-2"]


@pytest.mark.parametrize("snapshot", ["garbage", {"manifests": "x"}, {"manifests": [1]}, {"track_manifests": {"a": 1}}])
def test_a_malformed_snapshot_restores_nothing(snapshot):
    client = Tracks()
    client.add(Track.from_dict(service_tracks().videos[0].to_dict()))
    restore(client, snapshot)
    assert not client.videos[0].data.get("dash")
    assert parse(snapshot, URL, "en") is None


def test_a_saved_manifest_that_does_not_parse_gives_none():
    assert parse({"manifests": [{"type": "dash", "url": URL, "data": "bm90IHpsaWI="}]}, URL, "en") is None


def test_the_inflate_budget_covers_every_entry_of_one_restore(monkeypatch, caplog):
    saved = capture(service_tracks())
    entry = saved["manifests"][0]
    size = inflated_size(entry)
    saved["manifests"].append({**entry, "url": URL + "?copy"})
    monkeypatch.setattr(snapshot_module, "MAX_SNAPSHOT_BYTES", size)
    assert parse(saved, URL, "en") is not None

    client = Tracks()
    client.add(Track.from_dict(service_tracks().videos[0].to_dict()))
    with caplog.at_level(logging.WARNING, logger="snapshot"):
        restore(client, saved)
    assert client.videos[0].data.get("dash")
    assert ["expands beyond allowed size" in r.getMessage() for r in caplog.records] == [True]

    monkeypatch.setattr(snapshot_module, "MAX_SNAPSHOT_BYTES", size - 1)
    assert parse(saved, URL, "en") is None


def test_the_fragments_and_the_whole_manifests_share_one_budget(monkeypatch):
    tracks = service_tracks()
    detach_video(tracks)
    monkeypatch.setattr(snapshot_module, "MAX_SNAPSHOT_BYTES", inflated_size(capture(tracks)["manifests"][0]))
    client = round_trip(tracks)
    assert client.videos[0].data.get("dash")
    assert not client.audio[0].data.get("dash")


def test_compression_level_zero_restores():
    video = round_trip(service_tracks(), level=0).videos[0]
    assert stitched(video) == ["main-1", "main-2", "main-3"]


def test_an_export_from_a_remote_client_imports_every_track(tmp_path):
    tracks = service_tracks()
    detach_video(tracks)
    imported = export_and_import(tmp_path, round_trip(tracks))
    assert (len(imported.videos), len(imported.audio)) == (1, 1)
    assert stitched(imported.videos[0]) == ["main-1", "main-2", "main-3"]
