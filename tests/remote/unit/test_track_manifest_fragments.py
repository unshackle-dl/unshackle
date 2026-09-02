"""An AdaptationSet a service built itself survives the server-to-client round trip."""

from __future__ import annotations

import pytest
from langcodes import Language
from lxml import etree

from unshackle.core.api.handlers import extract_track_manifests
from unshackle.core.remote_service import resolve_track_manifests
from unshackle.core.tracks import Subtitle, Tracks

pytestmark = pytest.mark.unit

MPD = """<?xml version="1.0"?>
<MPD type="static" mediaPresentationDuration="PT10S">
  <BaseURL>https://cdn.example/base/</BaseURL>
  <Period id="0">
    <AdaptationSet contentType="text" lang="en">
      <Representation id="sub-0" bandwidth="1000">
        <SegmentTemplate media="$Number$.vtt" startNumber="1">
          <SegmentTimeline><S t="0" d="4"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""


def test_detached_adaptation_set_round_trips() -> None:
    manifest = etree.fromstring(MPD.encode())
    period = manifest.find("Period")
    # a service that merges segment timelines ends up with a copy detached from the tree
    combined = etree.fromstring(etree.tostring(period.find("AdaptationSet")))
    combined.find("Representation").find("SegmentTemplate").find("SegmentTimeline").append(
        etree.fromstring(b'<S t="4" d="4"/>')
    )

    track = Subtitle(
        id_="abc123",
        url="https://cdn.example/manifest.mpd",
        codec=Subtitle.Codec.WebVTT,
        language=Language.get("en"),
        descriptor=Subtitle.Descriptor.DASH,
        data={
            "dash": {
                "manifest": manifest,
                "period": period,
                "adaptation_set": combined,
                "representation": combined.find("Representation"),
            }
        },
    )
    tracks = Tracks([track])

    fragments = extract_track_manifests(tracks)
    assert len(fragments) == 1

    client_track = Subtitle(
        id_="abc123",
        url=track.url,
        codec=Subtitle.Codec.WebVTT,
        language=Language.get("en"),
        descriptor=Subtitle.Descriptor.URL,
    )
    resolve_track_manifests(Tracks([client_track]), fragments)

    dash = client_track.data["dash"]
    assert dash["manifest"].find("BaseURL").text == "https://cdn.example/base/"
    assert dash["representation_id"] == "sub-0"
    assert len(dash["representation"].find("SegmentTemplate").find("SegmentTimeline").findall("S")) == 2
    assert client_track.descriptor == Subtitle.Descriptor.DASH


def test_in_tree_adaptation_set_is_not_shipped() -> None:
    manifest = etree.fromstring(MPD.encode())
    period = manifest.find("Period")
    adaptation_set = period.find("AdaptationSet")
    track = Subtitle(
        id_="abc123",
        url="https://cdn.example/manifest.mpd",
        codec=Subtitle.Codec.WebVTT,
        language=Language.get("en"),
        descriptor=Subtitle.Descriptor.DASH,
        data={
            "dash": {
                "manifest": manifest,
                "period": period,
                "adaptation_set": adaptation_set,
                "representation": adaptation_set.find("Representation"),
            }
        },
    )
    assert extract_track_manifests(Tracks([track])) == []


def test_fragment_keeps_only_the_track_representation() -> None:
    manifest = etree.fromstring(MPD.encode())
    period = manifest.find("Period")
    combined = etree.fromstring(etree.tostring(period.find("AdaptationSet")))
    wanted = etree.fromstring(b'<Representation id="sub-1" bandwidth="2000"/>')
    combined.append(wanted)

    track = Subtitle(
        id_="abc123",
        url="https://cdn.example/manifest.mpd",
        codec=Subtitle.Codec.WebVTT,
        language=Language.get("en"),
        descriptor=Subtitle.Descriptor.DASH,
        data={"dash": {"manifest": manifest, "period": period, "adaptation_set": combined, "representation": wanted}},
    )

    client_track = Subtitle(
        id_="abc123",
        url=track.url,
        codec=Subtitle.Codec.WebVTT,
        language=Language.get("en"),
        descriptor=Subtitle.Descriptor.URL,
    )
    resolve_track_manifests(Tracks([client_track]), extract_track_manifests(Tracks([track])))

    dash = client_track.data["dash"]
    assert dash["representation_id"] == "sub-1"
    assert [r.get("id") for r in dash["adaptation_set"].findall("Representation")] == ["sub-1"]
