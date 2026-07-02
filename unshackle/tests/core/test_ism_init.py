"""Regression tests for ISM init-segment synthesis (ftyp + moov).

Smooth Streaming fragments carry no moov; the init box must be rebuilt from the
manifest CodecPrivateData before shaka/mp4decrypt can parse the stream. These
guard the byte-level box structure so a future downloader refactor cannot
silently drop it again (the c323db9 regression).
"""

from __future__ import annotations

import struct

import pytest

from unshackle.core.manifests.ism_init import (NAL_START_CODE, PIFF_SENC_UUID, box, build_avcc, build_dec3,
                                               build_hvcc, build_init_segment, full_box,
                                               parse_codec_private_data_colour, parse_hevc_sps_format,
                                               read_per_sample_iv_size, read_track_id, remove_emulation_prevention,
                                               split_nal_units, synthesize_aac_codec_private_data)

# Real CodecPrivateData taken from a Smooth Streaming manifest.
VIDEO_HEVC_CPD = (
    "0000000140010C01FFFF01600000030090000003000003009695980900000001420101016000000300900000"
    "030000030096A001E020064165959A4930BC05A80808082000007D20000BB801000000014401C172B66240"
)
# H.264 SPS+PPS (start-code delimited) for the AVC path.
VIDEO_AVC_CPD = "00000001674d401e9a6602800b76020000003e90000bb800f18311200000000168ebccb22c"
# 10-bit (Main 10) HEVC VPS+SPS+PPS minted with x265; ffprobe reads the
# synthesized init as "Main 10 / yuv420p10le".
VIDEO_HEVC10_CPD = (
    "0000000140010c01ffff02200000030090000003000003003c959809000000000142010102200000030090"
    "000003000003003ca00a080b9f6d96566924caf0168080000003008000000c8400000000014401c172b4624000"
)
# HEVC VPS+SPS+PPS minted with x265, explicit SPS VUI colour signalling:
# PQ (bt2020/smpte2084/bt2020nc), HLG (arib-std-b67) and BT.709 SDR.
VIDEO_HEVC_PQ_CPD = (
    "0000000140010c01ffff02200000030090000003000003001e9598090000000142010102200000030090000003000003001ea020"
    "8104d96566924caf016a12201208000003000800000300c840000000014401c172b42240"
)
VIDEO_HEVC_HLG_CPD = (
    "0000000140010c01ffff02200000030090000003000003001e9598090000000142010102200000030090000003000003001ea020"
    "8104d96566924caf016a12241208000003000800000300c840000000014401c172b42240"
)
VIDEO_HEVC_SDR_CPD = (
    "0000000140010c01ffff02200000030090000003000003001e9598090000000142010102200000030090000003000003001ea020"
    "8104d96566924caf016a02020208000003000800000300c840000000014401c172b42240"
)
# Real Dolby Vision (dvhe, DoViProfile "stn") CodecPrivateData from a Smooth
# manifest: NALs arrive SPS,PPS,VPS (VPS last) and the VUI colour triple is
# Unspecified (2,2,2) — DV is signalled by FourCC only, never by CICP.
VIDEO_HEVC_DV_CPD = (
    "00000001420101022000000300B00000030000030096A001E020021C4D9457B91CAF016E0404042800001F480002EE0401F4E1"
    "15EE7E0001312D00002FAF0C80000000014401C1ACBE0EC90000000140010C01FFFF022000000300B00000030000030096"
    "15C0C00000FA40001770200FA680"
)
AAC_LC_CPD = "1190"
# Real Smooth EC-3 CodecPrivateData: WAVEFORMATEXTENSIBLE extension (samples
# per block + channel mask + DD+ GUID) followed by the 5-byte dec3 payload.
EC3_CPD = "00063F000000AF87FBA7022DFB42A4D405CD93843BDD0600200F00"
KID = bytes.fromhex("09fd2bd778bb544785ed2322dc6a7d87")


def top_level_boxes(data: bytes) -> list[tuple[str, int]]:
    boxes, offset = [], 0
    while offset + 8 <= len(data):
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        box_type = data[offset + 4 : offset + 8].decode("latin1")
        if size == 1:
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
        if size == 0:
            size = len(data) - offset
        boxes.append((box_type, size))
        offset += size
    return boxes


def test_split_nal_units_drops_start_codes():
    nals = split_nal_units(bytes.fromhex(VIDEO_HEVC_CPD))
    # VPS (32), SPS (33), PPS (34) by HEVC NAL type = (first_byte >> 1) & 0x3F.
    assert [(n[0] >> 1) & 0x3F for n in nals] == [32, 33, 34]


def test_hevc_init_structure():
    init = build_init_segment(
        stream_type="video",
        fourcc="HVC1",
        codec_private_data=VIDEO_HEVC_CPD,
        timescale=10000000,
        width=3840,
        height=1600,
    )
    boxes = top_level_boxes(init)
    assert [b[0] for b in boxes] == ["ftyp", "moov"]
    assert boxes[0][1] + boxes[1][1] == len(init)
    assert b"hvcC" in init
    assert b"hvc1" in init
    # Unencrypted: no protection scheme boxes.
    assert b"encv" not in init and b"sinf" not in init


def test_avc_init_structure():
    init = build_init_segment(
        stream_type="video",
        fourcc="H264",
        codec_private_data=VIDEO_AVC_CPD,
        timescale=10000000,
        width=1280,
        height=720,
    )
    assert init[4:8] == b"ftyp"
    assert b"avcC" in init and b"avc1" in init


def test_aac_audio_init_structure():
    init = build_init_segment(
        stream_type="audio",
        fourcc="AACL",
        codec_private_data=AAC_LC_CPD,
        timescale=10000000,
        channels=2,
        sampling_rate=48000,
    )
    assert b"mp4a" in init and b"esds" in init
    assert b"smhd" in init  # sound media header, not video


def test_encrypted_init_has_cenc_boxes():
    init = build_init_segment(
        stream_type="video",
        fourcc="HVC1",
        codec_private_data=VIDEO_HEVC_CPD,
        timescale=10000000,
        width=3840,
        height=1600,
        kid=KID,
    )
    # Encrypted sample entry is wrapped: encv -> sinf(frma+schm+schi(tenc)).
    assert b"encv" in init
    assert b"sinf" in init and b"frma" in init and b"tenc" in init
    assert b"cenc" in init
    # The 16-byte default_KID must be embedded verbatim for shaka to map the key.
    assert KID in init
    # Original codec preserved inside frma for the muxer.
    assert b"hvc1" in init


def test_unsupported_codec_raises():
    # Unknown FourCC (e.g. VC-1); caller soft-fails to raw concat.
    with pytest.raises(NotImplementedError):
        build_init_segment(
            stream_type="video",
            fourcc="WVC1",
            codec_private_data="00063F00",
            timescale=10000000,
        )


def test_ec3_init_embeds_dec3_from_codec_private_data():
    init = build_init_segment(
        stream_type="audio",
        fourcc="EC-3",
        codec_private_data=EC3_CPD,
        timescale=10000000,
        channels=6,
        sampling_rate=48000,
    )
    assert b"ec-3" in init
    # dec3 payload = CodecPrivateData past the 22-byte WAVEFORMATEXTENSIBLE header.
    assert box(b"dec3", bytes.fromhex(EC3_CPD)[22:]) in init
    assert b"esds" not in init  # no MPEG-4 descriptor inside an ec-3 entry


def test_ec3_encrypted_wraps_enca_with_frma():
    init = build_init_segment(
        stream_type="audio",
        fourcc="EC-3",
        codec_private_data=EC3_CPD,
        timescale=10000000,
        channels=6,
        kid=KID,
    )
    assert b"enca" in init and b"sinf" in init and b"tenc" in init
    assert box(b"frma", b"ec-3") in init
    assert KID in init


def test_ec3_dec3_found_in_full_waveformatextensible():
    # Some services ship the full WAVEFORMATEX header (18 bytes) before the
    # extension; the dec3 payload still follows the DD+ GUID.
    full = b"\xfe\xff" + b"\x00" * 16 + bytes.fromhex(EC3_CPD)
    payload = bytes.fromhex(EC3_CPD)[22:]
    assert build_dec3(full) == box(b"dec3", payload)


def test_ec3_without_dolby_guid_builds_bare_entry():
    assert build_dec3(b"\x00\x06\x3f\x00") is None
    init = build_init_segment(
        stream_type="audio",
        fourcc="EC-3",
        codec_private_data="",
        timescale=10000000,
        channels=6,
    )
    assert b"ec-3" in init and b"dec3" not in init


def test_aac_codec_private_data_synthesis_matches_real_manifest():
    # 48 kHz stereo AAC-LC must produce 0x1190 — the exact ASC real manifests carry.
    assert synthesize_aac_codec_private_data("AACL", 48000, 2).hex() == "1190"


def test_aach_synthesis_signals_sbr():
    asc = synthesize_aac_codec_private_data("AACH", 24000, 2)
    assert len(asc) == 4
    assert asc[0] >> 3 == 0x05  # AOT 5 = SBR (HE-AAC)
    # Extension sampling frequency = core * 2 = 48 kHz (index 3).
    assert ((asc[1] & 0x01) << 1) | (asc[2] >> 7) == 0x03


def test_aac_init_without_codec_private_data_synthesizes_asc():
    init = build_init_segment(
        stream_type="audio",
        fourcc="AACL",
        codec_private_data="",
        timescale=10000000,
        channels=2,
        sampling_rate=48000,
    )
    assert b"mp4a" in init and b"esds" in init
    assert bytes.fromhex(AAC_LC_CPD) in init


def test_dolby_vision_uses_dvh1_sample_entry():
    init = build_init_segment(
        stream_type="video",
        fourcc="DVH1",
        codec_private_data=VIDEO_HEVC_CPD,
        timescale=10000000,
        width=3840,
        height=1600,
    )
    assert b"dvh1" in init and b"hvcC" in init
    assert b"hvc1" not in init


def test_davc_maps_to_avc1():
    init = build_init_segment(
        stream_type="video",
        fourcc="DAVC",
        codec_private_data=VIDEO_AVC_CPD,
        timescale=10000000,
    )
    assert b"avc1" in init and b"avcC" in init


def test_lowercase_fourcc_normalized():
    # Real manifests ship FourCC="hvc1" in lowercase.
    init = build_init_segment(
        stream_type="video",
        fourcc="hvc1",
        codec_private_data=VIDEO_HEVC_CPD,
        timescale=10000000,
    )
    assert b"hvcC" in init


def test_avcc_selects_sps_pps_by_nal_type_not_position():
    nals = split_nal_units(bytes.fromhex(VIDEO_AVC_CPD))
    swapped = NAL_START_CODE + nals[1] + NAL_START_CODE + nals[0]  # PPS first
    avcc = build_avcc(swapped)
    # Profile/compat/level must still come from the SPS body.
    assert avcc[9:12] == nals[0][1:4]


def test_nal_length_field_respected():
    avcc = build_avcc(bytes.fromhex(VIDEO_AVC_CPD), nal_length_size=2)
    # avcC payload byte 4 low 2 bits = lengthSizeMinusOne.
    assert avcc[12] & 0x03 == 1


def test_parse_hevc_sps_format_8bit():
    sps = split_nal_units(bytes.fromhex(VIDEO_HEVC_CPD))[1]
    assert parse_hevc_sps_format(remove_emulation_prevention(sps)) == (1, 0, 0)  # 4:2:0, 8-bit


def test_hvcc_signals_10bit_from_sps():
    sps = next(n for n in split_nal_units(bytes.fromhex(VIDEO_HEVC10_CPD)) if (n[0] >> 1) & 0x3F == 33)
    assert parse_hevc_sps_format(remove_emulation_prevention(sps)) == (1, 2, 2)  # 4:2:0, 10-bit
    payload = build_hvcc(bytes.fromhex(VIDEO_HEVC10_CPD))[8:]  # strip box header
    assert payload[16] == 0xFC | 0x01  # chromaFormat 4:2:0
    assert payload[17] == 0xF8 | 0x02  # bitDepthLumaMinus8 = 2
    assert payload[18] == 0xF8 | 0x02  # bitDepthChromaMinus8 = 2


def test_ttml_init_structure():
    init = build_init_segment(
        stream_type="text",
        fourcc="TTML",
        codec_private_data="",
        timescale=10000000,
        language="eng",
    )
    assert b"stpp" in init
    assert b"sthd" in init  # subtitle media header
    assert b"subt" in init and b"SubtitleHandler\0" in init
    assert b"http://www.w3.org/ns/ttml\0" in init


def test_constant_iv_tenc_form():
    constant_iv = bytes(range(16))
    init = build_init_segment(
        stream_type="video",
        fourcc="HVC1",
        codec_private_data=VIDEO_HEVC_CPD,
        timescale=10000000,
        kid=KID,
        constant_iv=constant_iv,
    )
    # Constant-IV form: default_Per_Sample_IV_Size = 0, then size + IV after the KID.
    assert KID + bytes([len(constant_iv)]) + constant_iv in init
    tenc_at = init.index(b"tenc")
    assert init[tenc_at + 4 + 4 + 3] == 0  # default_Per_Sample_IV_Size


def make_fragment(senc: bytes = b"", saiz: bytes = b"") -> bytes:
    tfhd = full_box(b"tfhd", 0, 0, struct.pack(">I", 1) + b"\x00" * 4)
    traf = box(b"traf", tfhd + senc + saiz)
    return box(b"moof", traf) + box(b"mdat", b"\x00" * 4)


def test_iv_size_from_piff_senc_override_flag():
    # PIFF senc uuid with flags&1: AlgorithmID(3) + IV_size(1) + KID(16) override.
    payload = b"\x00\x00\x00\x01" + b"\x00\x00\x01" + bytes([16]) + KID + struct.pack(">I", 0)
    senc = box(b"uuid", PIFF_SENC_UUID + payload)
    assert read_per_sample_iv_size(make_fragment(senc=senc)) == 16


def test_iv_size_from_senc_payload_length():
    # Standard senc, no subsamples: 3 samples x 8-byte IVs.
    senc = full_box(b"senc", 0, 0, struct.pack(">I", 3) + b"\x11" * 24)
    assert read_per_sample_iv_size(make_fragment(senc=senc)) == 8


def test_iv_size_from_senc_with_subsamples():
    # senc flags&2: per sample IV(8) + entry_count(2) + 6 bytes per entry.
    sample = b"\x22" * 8 + struct.pack(">H", 1) + b"\x00" * 6
    senc = full_box(b"senc", 0, 2, struct.pack(">I", 2) + sample * 2)
    assert read_per_sample_iv_size(make_fragment(senc=senc)) == 8


def test_iv_size_from_saiz_fallback():
    saiz = full_box(b"saiz", 0, 0, bytes([16]) + struct.pack(">I", 5))
    assert read_per_sample_iv_size(make_fragment(saiz=saiz)) == 16


def test_iv_size_undetermined_returns_none():
    assert read_per_sample_iv_size(make_fragment()) is None


def test_hvcc_embeds_vps_sps_pps():
    hvcc = build_hvcc(bytes.fromhex(VIDEO_HEVC_CPD))
    nals = split_nal_units(bytes.fromhex(VIDEO_HEVC_CPD))
    # Each original NAL unit (VPS/SPS/PPS) is embedded verbatim in the arrays.
    for nal in nals:
        assert nal in hvcc


def test_avcc_requires_sps_and_pps():
    with pytest.raises(ValueError):
        build_avcc(b"\x00\x00\x00\x01\x67only_sps")


def test_read_track_id_from_fragment():
    # Minimal moof/traf/tfhd carrying track_ID = 7.
    tfhd = full_box("tfhd".encode(), 0, 0, struct.pack(">I", 7) + b"\x00" * 4)
    traf = box(b"traf", tfhd)
    moof = box(b"moof", traf)
    mdat = box(b"mdat", b"\x00\x00")
    assert read_track_id(moof + mdat) == 7


def test_read_track_id_missing_returns_none():
    assert read_track_id(box(b"mdat", b"\x00\x00")) is None


def test_remove_emulation_prevention():
    # 00 00 03 XX -> the 0x03 emulation byte is dropped.
    assert remove_emulation_prevention(b"\x00\x00\x03\x01") == b"\x00\x00\x01"
    assert remove_emulation_prevention(b"\x00\x00\x03\x00\x00\x03\x96") == b"\x00\x00\x00\x00\x96"
    # The byte after a consumed escape is data, even another 0x03.
    assert remove_emulation_prevention(b"\x00\x00\x03\x03") == b"\x00\x00\x03"
    assert remove_emulation_prevention(b"\x00\x00\x03\x03\x00\x00\x03\x01") == b"\x00\x00\x03\x00\x00\x01"


def test_two_letter_or_uppercase_language_falls_back_to_und():
    # mdhd packs three a-z letters; "en"/"ENG" must not crash struct.pack.
    for lang in ("en", "ENG", "", "e1x"):
        init = build_init_segment(
            stream_type="audio",
            fourcc="AACL",
            codec_private_data=AAC_LC_CPD,
            timescale=10000000,
            language=lang,
        )
        assert init[4:8] == b"ftyp"


def test_high_sampling_rate_does_not_overflow():
    # 96 kHz exceeds the 16.16 integer field; written as 0 like ffmpeg does.
    init = build_init_segment(
        stream_type="audio",
        fourcc="AACL",
        codec_private_data="",
        timescale=10000000,
        sampling_rate=96000,
    )
    assert b"mp4a" in init


def test_read_track_id_truncated_tfhd_returns_none():
    tfhd = full_box(b"tfhd", 0, 0, b"\x00\x00")  # too short for a track_ID
    fragment = box(b"moof", box(b"traf", tfhd))
    assert read_track_id(fragment) is None


def test_parse_colour_hevc_pq():
    # PQ master: bt2020 primaries (9), smpte2084 transfer (16), bt2020nc matrix (9).
    assert parse_codec_private_data_colour("HVC1", bytes.fromhex(VIDEO_HEVC_PQ_CPD)) == (9, 16, 9)


def test_parse_colour_hevc_hlg():
    assert parse_codec_private_data_colour("HVC1", bytes.fromhex(VIDEO_HEVC_HLG_CPD)) == (9, 18, 9)


def test_parse_colour_hevc_bt709():
    assert parse_codec_private_data_colour("HVC1", bytes.fromhex(VIDEO_HEVC_SDR_CPD)) == (1, 1, 1)
    # The real-manifest 8-bit sample also signals BT.709 explicitly.
    assert parse_codec_private_data_colour("HVC1", bytes.fromhex(VIDEO_HEVC_CPD)) == (1, 1, 1)


def test_parse_colour_dv_is_unspecified():
    # DV carries no usable CICP; the DV decision must come from the FourCC.
    assert parse_codec_private_data_colour("DVHE", bytes.fromhex(VIDEO_HEVC_DV_CPD)) == (2, 2, 2)


def test_dv_cpd_with_vps_last_builds_init():
    cpd = bytes.fromhex(VIDEO_HEVC_DV_CPD)
    nals = split_nal_units(cpd)
    assert [(n[0] >> 1) & 0x3F for n in nals] == [33, 34, 32]  # SPS, PPS, VPS
    sps = remove_emulation_prevention(nals[0])
    assert parse_hevc_sps_format(sps) == (1, 2, 2)  # 4:2:0, 10-bit
    hvcc = build_hvcc(cpd)
    for nal in nals:
        assert nal in hvcc
    init = build_init_segment(
        stream_type="video",
        fourcc="DVHE",
        codec_private_data=VIDEO_HEVC_DV_CPD,
        timescale=10000000,
        width=3840,
        height=2160,
    )
    assert b"dvh1" in init and b"hvcC" in init


def test_parse_colour_absent_or_unknown_returns_none():
    # Real sample without a VUI colour description.
    assert parse_codec_private_data_colour("HVC1", bytes.fromhex(VIDEO_HEVC10_CPD)) is None
    # Non-HEVC codecs (AVC has no HDR deployment) and truncated data must not raise.
    assert parse_codec_private_data_colour("H264", bytes.fromhex(VIDEO_AVC_CPD)) is None
    assert parse_codec_private_data_colour("WVC1", bytes.fromhex(VIDEO_AVC_CPD)) is None
    assert parse_codec_private_data_colour("HVC1", b"\x00\x00\x00\x01\x42") is None


def test_hvcc_profile_tier_level_is_nonzero():
    # De-emulated PTL must yield real profile/level, not the off-by-one garbage.
    hvcc = build_hvcc(bytes.fromhex(VIDEO_HEVC_CPD))
    payload = hvcc[8:]  # strip box header
    profile_idc = payload[1] & 0x1F
    level_idc = payload[12]
    assert profile_idc != 0
    assert level_idc != 0
