"""Tests for the manifest data an import must rebuild before a DASH track can download.

``Track.to_dict`` cannot serialise manifest XML, so a rebuilt DASH track carries no
``track.data["dash"]``. ``DASH.download_track`` reads that mapping unconditionally, so any
DASH track the import hands back without it raises on the first segment. Two exports reach
that state: one with no top-level ``manifest_url``, and one whose ``manifest_url`` covers
only part of the ladder (a separate MPD per codec).
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any, Optional

import click
import pytest

from unshackle.core.import_service import ImportService
from unshackle.core.manifests import DASH

MPD_TEMPLATE = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10M">
  <Period id="1">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="{rep_id}" codecs="{codecs}" bandwidth="{bandwidth}" width="{width}" height="{height}">
        <BaseURL>v.mp4</BaseURL><SegmentBase indexRange="0-1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

AVC_URL = "https://example.invalid/avc/dash/m.mpd"
HEVC_URL = "https://example.invalid/hevc/dash/m.mpd"

MANIFESTS = {
    AVC_URL: MPD_TEMPLATE.format(rep_id="v-avc", codecs="avc1.640028", bandwidth=5000000, width=1920, height=1080),
    HEVC_URL: MPD_TEMPLATE.format(
        rep_id="v-hevc", codecs="hvc1.2.4.L153.B0", bandwidth=6000000, width=3840, height=2160
    ),
}


def exported_video(track_id: str, url: str, codec: str, width: int, height: int) -> dict[str, Any]:
    return {
        "type": "Video",
        "id": track_id,
        "url": url,
        "language": "en",
        "descriptor": "DASH",
        "codec": codec,
        "bitrate": 5000000 if codec == "AVC" else 6000000,
        "width": width,
        "height": height,
    }


def make_service(tmp_path: Path, manifest_url: Optional[str], tracks: dict[str, Any]) -> ImportService:
    entry: dict[str, Any] = {
        "meta": {"type": "movie", "id": "movie-1", "name": "Example Movie", "year": 2024, "language": "en"},
        "manifest_type": "DASH",
        "tracks": tracks,
    }
    if manifest_url:
        entry["manifest_url"] = manifest_url
    export = {"version": 2, "service": "EXAMPLE", "titles": {"movie-1": entry}}
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export), encoding="utf8")
    return ImportService(click.Context(click.Command("dl")), "EXAMPLE", "movie-1", str(path))


@pytest.fixture(autouse=True)
def local_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve each MPD from memory, so a test never depends on the network."""
    monkeypatch.setattr(DASH, "from_url", lambda url, session=None, **kw: DASH.from_text(MANIFESTS[url], url))


def test_export_without_manifest_url_still_rebuilds_dash_manifest_data(tmp_path: Path) -> None:
    """No top-level manifest_url means the export's own track URLs are the only MPDs to fetch."""
    service = make_service(tmp_path, None, {"v-avc": exported_video("v-avc", AVC_URL, "AVC", 1920, 1080)})
    tracks = service.get_tracks(next(iter(service.get_titles())))

    assert [t.id for t in tracks.videos] == ["v-avc"]
    # DASH.download_track reads all three of these; a missing one is a KeyError mid-download.
    assert set(tracks.videos[0].data.get("dash", {})) >= {"manifest", "adaptation_set", "representation"}


def test_exported_hevc_on_its_own_mpd_survives_the_reparse(tmp_path: Path) -> None:
    """A ladder split across per-codec MPDs loses every codec but manifest_url's if only URL tracks merge back."""
    service = make_service(
        tmp_path,
        AVC_URL,
        {"v-hevc": exported_video("v-hevc", HEVC_URL, "HEVC", 3840, 2160)},
    )
    tracks = service.get_tracks(next(iter(service.get_titles())))

    codecs = {t.codec.name for t in tracks.videos if t.codec}
    assert "HEVC" in codecs, "the exported HEVC track was dropped, so -v h.265 has nothing to select"
    hevc = next(t for t in tracks.videos if t.codec and t.codec.name == "HEVC")
    assert set(hevc.data.get("dash", {})) >= {"manifest", "adaptation_set", "representation"}
    # bound to its own manifest's representation, not just to any populated mapping
    assert hevc.data["dash"]["representation"].get("id") == "v-hevc"


SUB_MPD_URL = "https://example.invalid/subs/de.mpd"

MAIN_WITH_SUB = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10M">
  <Period id="1">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="v-avc" codecs="avc1.640028" bandwidth="5000000" width="1920" height="1080">
        <BaseURL>v.mp4</BaseURL><SegmentBase indexRange="0-1"/></Representation>
    </AdaptationSet>
    <AdaptationSet contentType="text" mimeType="text/vtt" lang="en">
      <Representation id="s-en" bandwidth="1000">
        <BaseURL>en.vtt</BaseURL><SegmentBase indexRange="0-1"/></Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

FOREIGN_SUB_MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10M">
  <Period id="1">
    <AdaptationSet contentType="text" mimeType="text/vtt" lang="de">
      <Representation id="s-de" bandwidth="1000">
        <BaseURL>de.vtt</BaseURL><SegmentBase indexRange="0-1"/></Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


def test_foreign_subtitle_adds_to_the_manifests_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a side-loaded (URL) subtitle replaces the re-parsed set.

    A subtitle that lives on its own MPD is an addition. Nothing about it says the service
    considered the main manifest's copy the worse one, and clearing on it deletes languages the
    export never meant to drop.
    """
    manifests = {AVC_URL: MAIN_WITH_SUB, SUB_MPD_URL: FOREIGN_SUB_MPD}
    monkeypatch.setattr(DASH, "from_url", lambda url, session=None, **kw: DASH.from_text(manifests[url], url))
    foreign = DASH.from_text(FOREIGN_SUB_MPD, SUB_MPD_URL).to_tracks(language="de").subtitles[0]

    service = make_service(
        tmp_path,
        AVC_URL,
        {
            foreign.id: {
                "type": "Subtitle",
                "id": foreign.id,
                "url": SUB_MPD_URL,
                "language": "de",
                "descriptor": "DASH",
                "codec": "WebVTT",
            }
        },
    )
    tracks = service.get_tracks(next(iter(service.get_titles())))

    assert sorted(str(t.language) for t in tracks.subtitles) == ["de", "en"], (
        "the main manifest's own subtitles were wiped by a subtitle from another MPD"
    )


TWO_RUNG_MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10M">
  <Period id="1">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="lo" codecs="avc1.640028" bandwidth="3000000" width="1920" height="1080">
        <BaseURL>lo.mp4</BaseURL><SegmentBase indexRange="0-1"/></Representation>
      <Representation id="hi" codecs="avc1.640028" bandwidth="{top}" width="1920" height="1080">
        <BaseURL>hi.mp4</BaseURL><SegmentBase indexRange="0-1"/></Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


@pytest.mark.parametrize(
    "exported_url",
    [AVC_URL, AVC_URL + "?token=EXPIRED"],
    ids=["same-url", "signed-url"],
)
def test_reencoded_top_rung_does_not_readd_the_exported_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exported_url: str
) -> None:
    """Track ids hash the bitrate, so re-encoding a rung makes the exported id look like a foreign MPD's.

    Merging it back on that basis duplicates the rung, and the attribute fallback then binds the
    copy to the first representation of the same codec and resolution: the 3 Mbps one. The result
    downloads `lo` under a 6 Mbps name.

    The signed variant is the same manifest reached by two strings: a service may fetch a signed
    URL while registering the bare one as Title.tracks.manifest_url, so comparing them whole
    reopens the duplicate.
    """
    exported = DASH.from_text(TWO_RUNG_MPD.format(top=6000000), exported_url).to_tracks(language="en")
    top = next(t for t in exported.videos if t.bitrate == 6000000)
    monkeypatch.setattr(
        DASH, "from_url", lambda url, session=None, **kw: DASH.from_text(TWO_RUNG_MPD.format(top=6500000), url)
    )

    service = make_service(tmp_path, AVC_URL, {top.id: exported_video(top.id, exported_url, "AVC", 1920, 1080)})
    tracks = service.get_tracks(next(iter(service.get_titles())))

    assert sorted(t.bitrate for t in tracks.videos) == [3000000, 6500000], (
        "the re-parsed ladder is the only source for this manifest's rungs"
    )
    bound = {t.data["dash"]["representation"].get("id") for t in tracks.videos}
    assert bound == {"lo", "hi"}, "two tracks were bound to the same representation"


def test_clearkey_license_hook_is_bindable(tmp_path: Path) -> None:
    """dl.py binds service.get_clearkey_license for every track, DRM or not, before downloading.

    Bind it the way dl.py does rather than asking hasattr: a non-callable attribute satisfies
    hasattr and still fails at the partial with a TypeError.
    """
    service = make_service(tmp_path, AVC_URL, {})
    bound = partial(service.get_clearkey_license, title=None, track=None)
    assert callable(bound)
    with pytest.raises(RuntimeError, match="keys come from the export"):
        bound(challenge=b"")
