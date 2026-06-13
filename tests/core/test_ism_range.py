"""Offline characterization: ISM CodecPrivateData SPS VUI -> Video.Range
(PQ -> HDR10, HLG -> HLG, BT.709/absent -> SDR). HDR10+ is per-frame SEI,
undecidable from the manifest; the post-mux bitstream probe names it."""

from __future__ import annotations

from unshackle.core.manifests import ISM
from unshackle.core.tracks import Video

from tests.core.test_ism_init import (VIDEO_HEVC10_CPD, VIDEO_HEVC_DV_CPD, VIDEO_HEVC_HLG_CPD,  # isort: skip
                                      VIDEO_HEVC_PQ_CPD, VIDEO_HEVC_SDR_CPD)


def manifest_xml(cpd: str, fourcc: str = "HVC1") -> str:
    return (
        '<SmoothStreamingMedia MajorVersion="2" MinorVersion="0" TimeScale="10000000" Duration="100000000">'
        '<StreamIndex Type="video" Name="video" Chunks="1" QualityLevels="1" MaxWidth="3840" MaxHeight="2160" '
        'Url="QualityLevels({bitrate})/Fragments(video={start time})">'
        f'<QualityLevel Index="0" Bitrate="15000000" FourCC="{fourcc}" MaxWidth="3840" MaxHeight="2160" '
        f'CodecPrivateData="{cpd}"/>'
        '<c t="0" d="100000000"/>'
        "</StreamIndex>"
        "</SmoothStreamingMedia>"
    )


def parse_video(cpd: str, fourcc: str = "HVC1") -> Video:
    tracks = ISM.from_text(manifest_xml(cpd, fourcc), url="https://x/ism/manifest").to_tracks(language="en")
    assert len(tracks.videos) == 1
    return tracks.videos[0]


def test_pq_codec_private_data_yields_hdr10() -> None:
    assert parse_video(VIDEO_HEVC_PQ_CPD).range == Video.Range.HDR10


def test_hlg_codec_private_data_yields_hlg() -> None:
    assert parse_video(VIDEO_HEVC_HLG_CPD).range == Video.Range.HLG


def test_bt709_codec_private_data_stays_sdr() -> None:
    assert parse_video(VIDEO_HEVC_SDR_CPD).range == Video.Range.SDR


def test_colourless_codec_private_data_defaults_sdr() -> None:
    # Real 10-bit sample without a VUI colour description: unspecified -> SDR.
    assert parse_video(VIDEO_HEVC10_CPD).range == Video.Range.SDR


def test_get_video_range_dolby_vision_fourcc() -> None:
    assert ISM.get_video_range("DVH1", VIDEO_HEVC_PQ_CPD) == Video.Range.DV
    assert ISM.get_video_range("DVHE", "") == Video.Range.DV


def test_dv_track_from_real_smooth_cpd() -> None:
    # Live manifests ship lowercase "dvhe"; its VUI is Unspecified so the
    # FourCC short-circuit is the only thing standing between DV and SDR.
    video = parse_video(VIDEO_HEVC_DV_CPD, fourcc="dvhe")
    assert video.range == Video.Range.DV
    assert ISM.get_video_range("hvc1", VIDEO_HEVC_DV_CPD) == Video.Range.SDR


def test_get_video_range_malformed_data_soft_fails_sdr() -> None:
    assert ISM.get_video_range("HVC1", "not-hex") == Video.Range.SDR
    assert ISM.get_video_range("HVC1", "") == Video.Range.SDR
    assert ISM.get_video_range("", VIDEO_HEVC_PQ_CPD) == Video.Range.SDR
