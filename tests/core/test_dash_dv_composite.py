from __future__ import annotations

import pytest

from unshackle.core.manifests import DASH
from unshackle.core.tracks import Video

MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:scte214="urn:scte:dash:scte214-extensions"
     type="static" mediaPresentationDuration="PT4S">
  <Period id="p0">
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <EssentialProperty schemeIdUri="urn:mpeg:mpegB:cicp:ColourPrimaries" value="9"/>
      <EssentialProperty schemeIdUri="urn:mpeg:mpegB:cicp:TransferCharacteristics" value="16"/>
      <EssentialProperty schemeIdUri="urn:mpeg:mpegB:cicp:MatrixCoefficients" value="9"/>
      <Representation id="hdr" codecs="hvc1.2.4.L150.90" width="3840" height="1920" bandwidth="7000000"
                      supplementalCodecs="dvh1.08.06" supplementalProfiles="db1p">
        <BaseURL>hdr.mp4</BaseURL><SegmentBase indexRange="0-99"/>
      </Representation>
      <Representation id="scte" codecs="hvc1.2.4.L120.90" width="1600" height="800" bandwidth="700000"
                      scte214:supplementalCodecs="dvh1.08.03">
        <BaseURL>scte.mp4</BaseURL><SegmentBase indexRange="0-99"/>
      </Representation>
      <Representation id="plain" codecs="hvc1.2.4.L90.90" width="1024" height="512" bandwidth="400000">
        <BaseURL>plain.mp4</BaseURL><SegmentBase indexRange="0-99"/>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <Representation id="dv" codecs="dvh1.05.06" width="3840" height="1920" bandwidth="6800000"
                      supplementalCodecs="dvh1.05.06">
        <BaseURL>dv.mp4</BaseURL><SegmentBase indexRange="0-99"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""


def test_a_dv_codec_in_supplemental_codecs_marks_an_hdr10_track_dv_compatible() -> None:
    videos = {
        v.data["dash"]["representation_id"]: v
        for v in DASH.from_text(MPD, url="https://example.test/m.mpd").to_tracks("en").videos
    }

    assert {k: (v.range, v.dv_compatible_bitstream) for k, v in videos.items()} == {
        "hdr": (Video.Range.HDR10, True),
        "scte": (Video.Range.HDR10, True),
        "plain": (Video.Range.HDR10, False),
        "dv": (Video.Range.DV, False),
    }
    assert "[H.265, HDR10+DV]" in str(videos["hdr"])
    assert "[H.265, HDR10]" in str(videos["plain"])


@pytest.mark.parametrize(
    ("codecs", "expected"),
    [
        ("dvh1.08.06/db1p", True),
        ("hvc1.2.4 dvhe.08.06", True),
        ("hvc1.2.4.L150.90,DVH1.08.06", True),
        ("cdm4", False),
        ("hvc1.2.4.L150.90", False),
        (None, False),
    ],
)
def test_has_dv_codec(codecs: str | None, expected: bool) -> None:
    assert Video.has_dv_codec(codecs) is expected


@pytest.mark.parametrize(
    ("codec", "codecs", "supplemental", "expected"),
    [
        (Video.Codec.HEVC, "hvc1.2.4.L150.90", "dvh1.08.06", True),
        (Video.Codec.HEVC, "hev1.2.4.L150.90", "dvhe.08.06/db1p", True),
        (Video.Codec.AVC, "avc1.640028", "dvav.09.05/db2g", False),
        (Video.Codec.HEVC, "hvc1.2.4.L150.90", "dvav.09.05", False),
        (Video.Codec.HEVC, "dvh1.05.06", "dvh1.05.06", False),
        (None, None, "dvh1.08.06", False),
    ],
)
def test_only_an_hevc_track_with_an_hevc_dv_supplemental_codec_is_dv_composite(
    codec: Video.Codec | None, codecs: str | None, supplemental: str, expected: bool
) -> None:
    assert Video.is_dv_composite(codec, codecs, supplemental) is expected


def test_an_hdr10p_track_with_a_dv_rpu_lists_as_hdr10p_plus_dv() -> None:
    video = Video(url="https://example.test/v.mp4", language="en", codec=Video.Codec.HEVC, range_=Video.Range.HDR10P)
    video.dv_compatible_bitstream = True
    assert "[H.265, HDR10P+DV]" in str(video)
