"""Export/import round trips for the two manifest types that are not DASH.

``get_tracks`` splits on manifest type, and the two halves need opposite things. ISM takes the
re-parse path and needs ``track.data["ism"]["segments"]`` restored, because
``ISM.download_track`` reads it. HLS takes the rebuild-from-dicts path and needs nothing
restored, because ``HLS.download_track`` re-fetches the variant from ``track.url``. That makes
the stored per-track URL load-bearing for HLS in a way it is not for ISM.

These drive the real ``dl.write_export`` rather than hand-written export dicts, so a change to
the export format fails here instead of silently invalidating the fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unshackle.commands.dl import dl
from unshackle.core.import_service import ImportService
from unshackle.core.manifests import HLS, ISM

ISM_URL = "https://cdn.example/x.ism/manifest"
ISM_MANIFEST = """<?xml version="1.0"?>
<SmoothStreamingMedia MajorVersion="2" MinorVersion="0" Duration="60000000" TimeScale="10000000">
  <StreamIndex Type="video" Name="video" Language="en"
               Url="QualityLevels({Bitrate})/Fragments(video={start_time})">
    <QualityLevel Index="0" Bitrate="3000000" FourCC="H264" MaxWidth="1280" MaxHeight="720"
                  CodecPrivateData="00000001674D401F"/>
    <c t="0" d="20000000" r="3"/>
  </StreamIndex>
  <StreamIndex Type="audio" Name="audio" Language="en"
               Url="QualityLevels({Bitrate})/Fragments(audio={start_time})">
    <QualityLevel Index="0" Bitrate="128000" AudioTag="255" SamplingRate="48000"
                  Channels="2" BitsPerSample="16" CodecPrivateData="1190"/>
    <c t="0" d="20000000" r="3"/>
  </StreamIndex>
</SmoothStreamingMedia>
"""

HLS_URL = "https://cdn.example/master.m3u8"
HLS_MANIFEST = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",LANGUAGE="en",NAME="English",DEFAULT=YES,URI="audio_en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="sub",LANGUAGE="en",NAME="English",URI="sub_en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2",AUDIO="aud",SUBTITLES="sub"
720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="aud",SUBTITLES="sub"
1080.m3u8
"""


def export_tracks(tmp_path: Path, tracks, manifest_url: str | None) -> Path:
    """Write a real export sidecar for a parsed ladder, as a finished download would."""
    from unshackle.core.titles import Movie

    class StubService:
        pass

    title = Movie(id_="movie-1", service=StubService, name="Example Movie", year=2024, language="en")
    for track in [*tracks.videos, *tracks.audio, *tracks.subtitles]:
        title.tracks.add(track)
    title.tracks.manifest_url = manifest_url

    runner = dl.__new__(dl)
    runner.service = "EXAMPLE"
    export = tmp_path / "export.json"
    for track in [*title.tracks.videos, *title.tracks.audio, *title.tracks.subtitles]:
        runner.write_export(export, title, track)
    return export


def import_tracks(export: Path):
    svc = ImportService(SimpleNamespace(parent=None, params={}), "EXAMPLE", "movie-1", str(export))
    return svc, svc.get_tracks(next(iter(svc.get_titles())))


@pytest.fixture
def ism_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ISM, "from_url", lambda url, session=None, **kw: ISM.from_text(ISM_MANIFEST, url))


@pytest.mark.parametrize("manifest_url", [ISM_URL, None], ids=["with-manifest-url", "without-manifest-url"])
def test_ism_round_trip_restores_segment_urls(tmp_path: Path, ism_offline: None, manifest_url: str | None) -> None:
    """ISM.download_track reads data["ism"]["segments"], which to_dict cannot carry across an export.

    Without a manifest_url the export's own track URLs are the only way back to the manifest,
    which is the case the re-parse branch alone never covered.
    """
    original = ISM.from_text(ISM_MANIFEST, ISM_URL).to_tracks(language="en")
    expected = {t.id: t.data["ism"]["segments"] for t in [*original.videos, *original.audio]}

    _, tracks = import_tracks(export_tracks(tmp_path, original, manifest_url))

    rebuilt = {t.id: t.data.get("ism", {}).get("segments") for t in [*tracks.videos, *tracks.audio]}
    assert rebuilt == expected, "every ISM track must come back with the fragment URLs it was exported with"


ISM_ENCRYPTED = """<?xml version="1.0"?>
<SmoothStreamingMedia MajorVersion="2" MinorVersion="0" Duration="60000000" TimeScale="10000000">
  <Protection><ProtectionHeader SystemID="{9A04F079-9840-4286-AB92-E65BE0885F95}">dGVzdA==</ProtectionHeader></Protection>
  <StreamIndex Type="video" Name="video" Language="en" Url="QualityLevels({Bitrate})/Fragments(video={start_time})">
    <QualityLevel Index="0" Bitrate="3000000" FourCC="H264" MaxWidth="1280" MaxHeight="720"
                  CodecPrivateData="00000001674D401F"/>
    <QualityLevel Index="1" Bitrate="6000000" FourCC="H264" MaxWidth="1920" MaxHeight="1080"
                  CodecPrivateData="00000001674D401F"/>
    <c t="0" d="20000000" r="3"/>
  </StreamIndex>
</SmoothStreamingMedia>
"""


def test_encrypted_ism_rung_gets_the_exported_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_server_keys must recognise an ISM manifest's protection, not only a DASH one.

    ISM.download_track reads only ``track.drm``, so a rung that misses key injection downloads
    encrypted and muxes without error. DASH survives the same omission because it re-derives its
    DRM from the manifest elements at download time.
    """
    monkeypatch.setattr(ISM, "from_url", lambda url, session=None, **kw: ISM.from_text(ISM_ENCRYPTED, url))
    parsed = ISM.from_text(ISM_ENCRYPTED, ISM_URL).to_tracks(language="en")

    # export the licensed rung only, as a finished download would
    licensed = max(parsed.videos, key=lambda t: t.bitrate or 0)
    export = export_tracks(tmp_path, parsed, ISM_URL)
    doc = json.loads(export.read_text(encoding="utf8"))
    doc["titles"]["movie-1"]["tracks"][licensed.id]["keys"] = {"0" * 32: "ab" * 16}
    export.write_text(json.dumps(doc), encoding="utf8")

    svc, tracks = import_tracks(export)
    movie = next(iter(svc.get_titles()))
    movie.tracks = tracks
    svc.resolve_server_keys(movie)

    other = min(tracks.videos, key=lambda t: t.bitrate or 0)
    assert svc.track_is_encrypted(other), "an ISM ProtectionHeader means the rung is encrypted"
    assert other.drm and other.drm[0].content_keys, "the unlicensed rung never received the exported key"


def test_hls_round_trip_keeps_every_variant_url(tmp_path: Path) -> None:
    """HLS re-fetches each variant from track.url at download time, so the stored URL has to survive."""
    original = HLS.from_text(HLS_MANIFEST, HLS_URL).to_tracks(language="en")
    expected = {t.id: str(t.url) for t in [*original.videos, *original.audio, *original.subtitles]}

    _, tracks = import_tracks(export_tracks(tmp_path, original, HLS_URL))

    rebuilt = {t.id: str(t.url) for t in [*tracks.videos, *tracks.audio, *tracks.subtitles]}
    assert rebuilt == expected, "the whole exported ladder must survive, each with its own variant URL"
    assert {t.descriptor.name for t in tracks} == {"HLS"}


def test_hls_import_never_refetches_the_master_playlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A master playlist can hand out a fresh token per fetch, so import must not depend on it.

    This is why HLS is absent from MANIFEST_DATA_KEYS. Re-fetching would add nothing and give a
    long-expired export another way to fail.
    """
    original = HLS.from_text(HLS_MANIFEST, HLS_URL).to_tracks(language="en")
    export = export_tracks(tmp_path, original, HLS_URL)

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("import re-fetched an HLS manifest")

    monkeypatch.setattr(HLS, "from_url", explode)
    monkeypatch.setattr(HLS, "from_text", explode)

    _, tracks = import_tracks(export)
    assert len(list(tracks)) == 4


def test_hls_tracks_tolerate_the_empty_manifest_data(tmp_path: Path) -> None:
    """HLS.download_track assigns into track.data["hls"] without creating it first.

    Track.data is a defaultdict, so the assignment auto-vivifies rather than raising. Pin that,
    because a plain dict here would turn every HLS import into a KeyError mid-download.
    """
    original = HLS.from_text(HLS_MANIFEST, HLS_URL).to_tracks(language="en")
    _, tracks = import_tracks(export_tracks(tmp_path, original, HLS_URL))

    track = tracks.videos[0]
    assert not track.data.get("hls"), "to_dict cannot carry the m3u8 objects, and does not need to"
    track.data["hls"]["segment_durations"] = [1, 2, 3]  # hls.py, in download_track
    assert track.data["hls"]["segment_durations"] == [1, 2, 3]
