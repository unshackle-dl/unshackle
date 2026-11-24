"""Regression tests for ISM init-segment synthesis (ftyp + moov).

Smooth Streaming fragments carry no moov; the init box must be rebuilt from the
manifest CodecPrivateData before shaka/mp4decrypt can parse the stream. These
guard the byte-level box structure so a future downloader refactor cannot
silently drop it again (the c323db9 regression).
"""

from __future__ import annotations

import struct

import pytest

from unshackle.core.manifests.ism_init import (
    NAL_START_CODE,
    PIFF_SENC_UUID,
    box,
    build_avcc,
    build_dec3,
    build_dvcc,
    build_hvcc,
    build_init_segment,
    full_box,
    parse_codec_private_data_colour,
    parse_codec_private_data_vui,
    parse_hevc_sps_format,
    piff_senc_to_cenc,
    read_per_sample_iv_size,
    read_track_id,
    remove_emulation_prevention,
    split_nal_units,
    synthesize_aac_codec_private_data,
)

VIDEO_HEVC_CPD = (
    "0000000140010C01FFFF01600000030090000003000003009695980900000001420101016000000300900000"
    "030000030096A001E020064165959A4930BC05A80808082000007D20000BB801000000014401C172B66240"
)
VIDEO_AVC_CPD = "00000001674d401e9a6602800b76020000003e90000bb800f18311200000000168ebccb22c"
VIDEO_HEVC10_CPD = (
    "0000000140010c01ffff02200000030090000003000003003c959809000000000142010102200000030090"
    "000003000003003ca00a080b9f6d96566924caf0168080000003008000000c8400000000014401c172b4624000"
)
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
VIDEO_HEVC_DV_CPD = (
    "00000001420101022000000300B00000030000030096A001E020021C4D9457B91CAF016E0404042800001F480002EE0401F4E1"
    "15EE7E0001312D00002FAF0C80000000014401C1ACBE0EC90000000140010C01FFFF022000000300B00000030000030096"
    "15C0C00000FA40001770200FA680"
)
AAC_LC_CPD = "1190"
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
    assert b"encv" not in init and b"sinf" not in init


CONTAINER_BOXES = {"moov", "trak", "mdia", "minf", "stbl", "dinf", "mvex", "sinf", "schi"}


def walk_box(data: bytes, start: int, end: int, path: str = "") -> None:
    """Assert every container's declared size spans its children exactly."""
    offset = start
    while offset < end:
        size = struct.unpack_from(">I", data, offset)[0]
        name = data[offset + 4 : offset + 8].decode("latin1")
        where = f"{path}/{name}"
        assert size >= 8, f"{where}: size {size}"
        assert offset + size <= end, f"{where}: overruns its parent"
        if name in CONTAINER_BOXES:
            walk_box(data, offset + 8, offset + size, where)
        offset += size
    assert offset == end, f"{path or '/'}: children do not span the parent exactly"


def find_stsd(init: bytes) -> int:
    """Offset of the stsd box, found by walking the box tree."""
    offset, end = 0, len(init)
    for name in ("moov", "trak", "mdia", "minf", "stbl", "stsd"):
        cursor = offset
        while cursor < end:
            size = struct.unpack_from(">I", init, cursor)[0]
            if init[cursor + 4 : cursor + 8].decode("latin1") == name:
                offset, end = cursor + 8, cursor + size
                break
            cursor += size
        else:
            raise AssertionError(f"{name} not found")
    return offset - 8


def sample_entry_bounds(init: bytes) -> tuple[int, int]:
    """(start, end) byte range of the first stsd sample entry."""
    stsd = find_stsd(init)
    entry_size = struct.unpack_from(">I", init, stsd + 16)[0]
    return stsd + 16, stsd + 16 + entry_size


STSD_CASES = [
    ("video", "HVC1", VIDEO_HEVC_CPD, KID, b"encv", {"width": 3840, "height": 2160}),
    ("video", "HVC1", VIDEO_HEVC_CPD, None, b"hvc1", {"width": 3840, "height": 2160}),
    ("video", "H264", VIDEO_AVC_CPD, None, b"avc1", {"width": 1280, "height": 720}),
    ("audio", "AACL", AAC_LC_CPD, None, b"mp4a", {"channels": 2}),
    ("audio", "EC-3", EC3_CPD, KID, b"enca", {"channels": 6}),
    ("text", "TTML", "", None, b"stpp", {}),
]


@pytest.mark.parametrize("stream_type,fourcc,cpd,kid,expected_4cc,extra", STSD_CASES)
def test_stsd_carries_two_entries_for_sample_description_index_2(stream_type, fourcc, cpd, kid, expected_4cc, extra):
    init = build_init_segment(
        stream_type=stream_type,
        fourcc=fourcc,
        codec_private_data=cpd,
        timescale=10000000,
        kid=kid,
        **extra,
    )
    offset = find_stsd(init)
    assert struct.unpack_from(">I", init, offset + 12)[0] == 2, "index 2 would dangle"

    entries, cursor = [], offset + 16
    for _ in range(2):
        size = struct.unpack_from(">I", init, cursor)[0]
        entries.append(init[cursor : cursor + size])
        cursor += size
    assert [e[4:8] for e in entries] == [expected_4cc, expected_4cc]
    assert struct.unpack_from(">I", init, offset)[0] == 16 + sum(len(e) for e in entries)
    walk_box(init, 0, len(init))


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
    assert b"smhd" in init


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
    assert b"encv" in init
    assert b"sinf" in init and b"frma" in init and b"tenc" in init
    assert b"cenc" in init
    assert KID in init
    assert b"hvc1" in init


def test_unsupported_codec_raises():
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
    assert box(b"dec3", bytes.fromhex(EC3_CPD)[22:]) in init
    assert b"esds" not in init


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
    assert synthesize_aac_codec_private_data("AACL", 48000, 2).hex() == "1190"


def test_aach_synthesis_signals_sbr():
    asc = synthesize_aac_codec_private_data("AACH", 24000, 2)
    assert len(asc) == 4
    assert asc[0] >> 3 == 0x05
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
    init = build_init_segment(
        stream_type="video",
        fourcc="hvc1",
        codec_private_data=VIDEO_HEVC_CPD,
        timescale=10000000,
    )
    assert b"hvcC" in init


def test_avcc_selects_sps_pps_by_nal_type_not_position():
    nals = split_nal_units(bytes.fromhex(VIDEO_AVC_CPD))
    swapped = NAL_START_CODE + nals[1] + NAL_START_CODE + nals[0]
    avcc = build_avcc(swapped)
    assert avcc[9:12] == nals[0][1:4]


def test_nal_length_field_respected():
    avcc = build_avcc(bytes.fromhex(VIDEO_AVC_CPD), nal_length_size=2)
    assert avcc[12] & 0x03 == 1


def test_parse_hevc_sps_format_8bit():
    sps = split_nal_units(bytes.fromhex(VIDEO_HEVC_CPD))[1]
    assert parse_hevc_sps_format(remove_emulation_prevention(sps)) == (1, 0, 0)


def test_hvcc_signals_10bit_from_sps():
    sps = next(n for n in split_nal_units(bytes.fromhex(VIDEO_HEVC10_CPD)) if (n[0] >> 1) & 0x3F == 33)
    assert parse_hevc_sps_format(remove_emulation_prevention(sps)) == (1, 2, 2)
    payload = build_hvcc(bytes.fromhex(VIDEO_HEVC10_CPD))[8:]
    assert payload[16] == 0xFC | 0x01
    assert payload[17] == 0xF8 | 0x02
    assert payload[18] == 0xF8 | 0x02


def test_ttml_init_structure():
    init = build_init_segment(
        stream_type="text",
        fourcc="TTML",
        codec_private_data="",
        timescale=10000000,
        language="eng",
    )
    assert b"stpp" in init
    assert b"sthd" in init
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
    assert KID + bytes([len(constant_iv)]) + constant_iv in init
    tenc_at = init.index(b"tenc")
    assert init[tenc_at + 4 + 4 + 3] == 0


def make_fragment(senc: bytes = b"", saiz: bytes = b"") -> bytes:
    tfhd = full_box(b"tfhd", 0, 0, struct.pack(">I", 1) + b"\x00" * 4)
    traf = box(b"traf", tfhd + senc + saiz)
    return box(b"moof", traf) + box(b"mdat", b"\x00" * 4)


def test_iv_size_from_piff_senc_override_flag():
    payload = b"\x00\x00\x00\x01" + b"\x00\x00\x01" + bytes([16]) + KID + struct.pack(">I", 0)
    senc = box(b"uuid", PIFF_SENC_UUID + payload)
    assert read_per_sample_iv_size(make_fragment(senc=senc)) == 16


def test_iv_size_from_senc_payload_length():
    senc = full_box(b"senc", 0, 0, struct.pack(">I", 3) + b"\x11" * 24)
    assert read_per_sample_iv_size(make_fragment(senc=senc)) == 8


def test_iv_size_from_senc_with_subsamples():
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
    for nal in nals:
        assert nal in hvcc


def test_avcc_requires_sps_and_pps():
    with pytest.raises(ValueError):
        build_avcc(b"\x00\x00\x00\x01\x67only_sps")


def test_read_track_id_from_fragment():
    tfhd = full_box("tfhd".encode(), 0, 0, struct.pack(">I", 7) + b"\x00" * 4)
    traf = box(b"traf", tfhd)
    moof = box(b"moof", traf)
    mdat = box(b"mdat", b"\x00\x00")
    assert read_track_id(moof + mdat) == 7


def test_read_track_id_missing_returns_none():
    assert read_track_id(box(b"mdat", b"\x00\x00")) is None


def test_remove_emulation_prevention():
    assert remove_emulation_prevention(b"\x00\x00\x03\x01") == b"\x00\x00\x01"
    assert remove_emulation_prevention(b"\x00\x00\x03\x00\x00\x03\x96") == b"\x00\x00\x00\x00\x96"
    assert remove_emulation_prevention(b"\x00\x00\x03\x03") == b"\x00\x00\x03"
    assert remove_emulation_prevention(b"\x00\x00\x03\x03\x00\x00\x03\x01") == b"\x00\x00\x03\x00\x00\x01"


def test_two_letter_or_uppercase_language_falls_back_to_und():
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
    init = build_init_segment(
        stream_type="audio",
        fourcc="AACL",
        codec_private_data="",
        timescale=10000000,
        sampling_rate=96000,
    )
    assert b"mp4a" in init


def test_read_track_id_truncated_tfhd_returns_none():
    tfhd = full_box(b"tfhd", 0, 0, b"\x00\x00")
    fragment = box(b"moof", box(b"traf", tfhd))
    assert read_track_id(fragment) is None


def test_parse_colour_hevc_pq():
    assert parse_codec_private_data_colour("HVC1", bytes.fromhex(VIDEO_HEVC_PQ_CPD)) == (9, 16, 9)


def test_parse_colour_hevc_hlg():
    assert parse_codec_private_data_colour("HVC1", bytes.fromhex(VIDEO_HEVC_HLG_CPD)) == (9, 18, 9)


def test_parse_colour_hevc_bt709():
    assert parse_codec_private_data_colour("HVC1", bytes.fromhex(VIDEO_HEVC_SDR_CPD)) == (1, 1, 1)
    assert parse_codec_private_data_colour("HVC1", bytes.fromhex(VIDEO_HEVC_CPD)) == (1, 1, 1)


def test_parse_colour_dv_is_unspecified():
    assert parse_codec_private_data_colour("DVHE", bytes.fromhex(VIDEO_HEVC_DV_CPD)) == (2, 2, 2)


def test_dv_cpd_with_vps_last_builds_init():
    cpd = bytes.fromhex(VIDEO_HEVC_DV_CPD)
    nals = split_nal_units(cpd)
    assert [(n[0] >> 1) & 0x3F for n in nals] == [33, 34, 32]
    sps = remove_emulation_prevention(nals[0])
    assert parse_hevc_sps_format(sps) == (1, 2, 2)
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
    assert parse_codec_private_data_colour("HVC1", bytes.fromhex(VIDEO_HEVC10_CPD)) is None
    assert parse_codec_private_data_colour("H264", bytes.fromhex(VIDEO_AVC_CPD)) is None
    assert parse_codec_private_data_colour("WVC1", bytes.fromhex(VIDEO_AVC_CPD)) is None
    assert parse_codec_private_data_colour("HVC1", b"\x00\x00\x00\x01\x42") is None


def test_parse_vui_fps():
    assert parse_codec_private_data_vui("HVC1", bytes.fromhex(VIDEO_HEVC_CPD))[1] == pytest.approx(24000 / 1001)
    assert parse_codec_private_data_vui("HVC1", bytes.fromhex(VIDEO_HEVC_PQ_CPD))[1] == 25.0
    assert parse_codec_private_data_vui("HVC1", bytes.fromhex(VIDEO_HEVC10_CPD)) == (None, 25.0)
    assert parse_codec_private_data_vui("H264", bytes.fromhex(VIDEO_AVC_CPD)) == (None, None)


def test_hvcc_profile_tier_level_is_nonzero():
    hvcc = build_hvcc(bytes.fromhex(VIDEO_HEVC_CPD))
    payload = hvcc[8:]
    profile_idc = payload[1] & 0x1F
    level_idc = payload[12]
    assert profile_idc != 0
    assert level_idc != 0


def piff_fragment(flags: int = 0x0, override: bool = False) -> bytes:
    """Minimal moof+mdat fragment carrying sample encryption in the PIFF uuid box."""
    ivs = b"".join(bytes([i]) * 8 for i in range(1, 3))
    senc_payload = struct.pack(">I", 2) + ivs
    if override:
        flags |= 0x1
        senc_payload = b"\x00\x00\x01" + bytes([8]) + KID + senc_payload
    uuid_box = box(b"uuid", PIFF_SENC_UUID + bytes([0]) + flags.to_bytes(3, "big") + senc_payload)
    tfhd = full_box(b"tfhd", 0, 0x20000, struct.pack(">I", 1))
    trun = full_box(b"trun", 0, 0x1, struct.pack(">II", 2, 0))
    traf = box(b"traf", tfhd + trun + uuid_box)
    moof = box(b"moof", full_box(b"mfhd", 0, 0, struct.pack(">I", 1)) + traf)
    moof = moof.replace(struct.pack(">II", 2, 0), struct.pack(">II", 2, len(moof) + 8), 1)
    return moof + box(b"mdat", b"\xab" * 64)


def test_piff_senc_rewritten_to_cenc_senc():
    frag = piff_fragment()
    out = piff_senc_to_cenc(frag)
    assert len(out) == len(frag)
    assert PIFF_SENC_UUID not in out
    assert b"senc" in out and b"free" in out
    assert struct.pack(">I", 2) + b"".join(bytes([i]) * 8 for i in range(1, 3)) in out
    assert out[:8] == frag[:8]
    assert out[len(out) - 72 :] == frag[len(frag) - 72 :]


def test_piff_senc_override_header_stripped():
    frag = piff_fragment(override=True)
    out = piff_senc_to_cenc(frag)
    assert len(out) == len(frag)
    assert PIFF_SENC_UUID not in out
    senc = out.index(b"senc")
    assert out[senc + 4] == 0
    assert int.from_bytes(out[senc + 5 : senc + 8], "big") & 0x1 == 0
    assert out[senc + 8 : senc + 12] == struct.pack(">I", 2)


def test_piff_subsample_flag_survives_override_strip():
    frag = piff_fragment(flags=0x2, override=True)
    out = piff_senc_to_cenc(frag)
    senc = out.index(b"senc")
    flags = int.from_bytes(out[senc + 5 : senc + 8], "big")
    assert flags & 0x2, "subsample-encryption flag must survive"
    assert not flags & 0x1, "override flag must be cleared"


def test_piff_rewrite_leaves_non_piff_fragments_alone():
    plain = box(b"moof", box(b"traf", full_box(b"tfhd", 0, 0, struct.pack(">I", 1)))) + box(b"mdat", b"\x00" * 16)
    assert piff_senc_to_cenc(plain) is plain
    assert piff_senc_to_cenc(b"") == b""


def test_piff_rewrite_skips_traf_that_already_has_senc():
    ivs = b"".join(bytes([i]) * 8 for i in range(1, 3))
    senc = full_box(b"senc", 0, 0, struct.pack(">I", 2) + ivs)
    uuid_box = box(b"uuid", PIFF_SENC_UUID + bytes([0]) + (0).to_bytes(3, "big") + struct.pack(">I", 2) + ivs)
    frag = box(b"moof", box(b"traf", senc + uuid_box)) + box(b"mdat", b"\x00" * 16)
    assert piff_senc_to_cenc(frag) is frag


def test_piff_rewrite_covers_every_moof_and_traf():
    frag = piff_fragment() + piff_fragment()
    out = piff_senc_to_cenc(frag)
    assert len(out) == len(frag)
    assert PIFF_SENC_UUID not in out
    assert out.count(b"senc") == 2


def test_piff_rewrite_refuses_non_ctr_algorithm_id():
    ivs = b"".join(bytes([i]) * 8 for i in range(1, 3))
    payload = (2).to_bytes(3, "big") + bytes([8]) + KID + struct.pack(">I", 2) + ivs
    uuid_box = box(b"uuid", PIFF_SENC_UUID + bytes([0]) + (0x1).to_bytes(3, "big") + payload)
    frag = box(b"moof", box(b"traf", uuid_box)) + box(b"mdat", b"\x00" * 16)
    assert piff_senc_to_cenc(frag) is frag


def traf_children_tile_exactly(frag: bytes) -> bool:
    """Every traf child's declared size must chain exactly to the traf end."""
    moof_size = struct.unpack(">I", frag[:4])[0]
    traf_start = 8
    while frag[traf_start + 4 : traf_start + 8] != b"traf":
        traf_start += struct.unpack(">I", frag[traf_start : traf_start + 4])[0]
    traf_end = traf_start + struct.unpack(">I", frag[traf_start : traf_start + 4])[0]
    assert traf_end <= moof_size
    offset = traf_start + 8
    while offset < traf_end:
        size = struct.unpack(">I", frag[offset : offset + 4])[0]
        if size < 8 or offset + size > traf_end:
            return False
        offset += size
    return offset == traf_end


def test_piff_rewrite_keeps_traf_children_tiling_exactly():
    assert traf_children_tile_exactly(piff_fragment())
    for frag in (piff_fragment(), piff_fragment(flags=0x2), piff_fragment(flags=0x2, override=True)):
        assert traf_children_tile_exactly(piff_senc_to_cenc(frag)), "rewritten traf no longer tiles"


def test_piff_rewrite_refuses_override_iv_size_mismatch():
    ivs = b"".join(bytes([i]) * 16 for i in range(1, 3))
    payload = (1).to_bytes(3, "big") + bytes([16]) + KID + struct.pack(">I", 2) + ivs
    uuid_box = box(b"uuid", PIFF_SENC_UUID + bytes([0]) + (0x1).to_bytes(3, "big") + payload)
    frag = box(b"moof", box(b"traf", uuid_box)) + box(b"mdat", b"\x00" * 16)
    assert piff_senc_to_cenc(frag, iv_size=8) is frag, "8-byte tenc must reject a 16-byte override"
    assert piff_senc_to_cenc(frag, iv_size=16) is not frag, "matching width must still rewrite"


def test_piff_rewrite_never_grows_output_on_malformed_boxes():
    good = piff_fragment()
    truncations = [good[:n] for n in range(8, len(good), 7)]
    oversized = bytearray(good)
    struct.pack_into(">I", oversized, 0, len(good) + 4096)
    for frag in [*truncations, bytes(oversized), b"", b"\x00" * 12]:
        out = piff_senc_to_cenc(frag)
        assert len(out) == len(frag), f"output grew: {len(frag)} -> {len(out)}"


def text_fragment(seq: int, begin: str, end: str, text: str, sdi: int) -> bytes:
    """A single-sample fTTML moof+mdat, mirroring what a Smooth text stream serves."""
    payload = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<tt xmlns='http://www.w3.org/ns/ttml' xml:lang='en'><body><div>"
        f"<p begin='{begin}' end='{end}'>{text}</p>"
        "</div></body></tt>"
    ).encode()
    mfhd = full_box(b"mfhd", 0, 0, struct.pack(">I", seq))
    tfhd = full_box(b"tfhd", 0, 0x020002, struct.pack(">I", 1) + struct.pack(">I", sdi))
    tfdt = full_box(b"tfdt", 1, 0, struct.pack(">Q", 0))
    trun = full_box(b"trun", 0, 0x000201, struct.pack(">I", 1) + struct.pack(">i", 0) + struct.pack(">I", len(payload)))
    return box(b"moof", mfhd + box(b"traf", tfhd + tfdt + trun)) + box(b"mdat", payload)


def test_unity_matrix_is_nine_int32s():
    from unshackle.core.manifests.ism_init import UNITY_MATRIX

    assert len(UNITY_MATRIX) == 36


@pytest.mark.parametrize("stream_type", ["video", "audio", "text"])
def test_mvhd_tkhd_sizes_match_iso_layout(stream_type):
    kwargs = {
        "video": dict(fourcc="H264", codec_private_data=VIDEO_AVC_CPD, width=1920, height=1080),
        "audio": dict(fourcc="AACL", codec_private_data=AAC_LC_CPD),
        "text": dict(fourcc="TTML", codec_private_data=""),
    }[stream_type]
    init = build_init_segment(stream_type=stream_type, duration=6_000_000_000, language="eng", **kwargs)

    sizes = {}
    for name in (b"mvhd", b"tkhd"):
        at = init.index(name) - 4
        sizes[name] = struct.unpack(">I", init[at : at + 4])[0]
    assert sizes[b"mvhd"] == 120
    assert sizes[b"tkhd"] == 104


def test_tkhd_carries_video_dimensions():
    init = build_init_segment(
        stream_type="video", fourcc="H264", codec_private_data=VIDEO_AVC_CPD, width=1920, height=1080
    )
    at = init.index(b"tkhd") - 4
    tkhd = init[at : at + struct.unpack(">I", init[at : at + 4])[0]]
    width, height = struct.unpack(">II", tkhd[-8:])
    assert (width >> 16, height >> 16) == (1920, 1080)


def test_pymp4_consumes_whole_init_and_reaches_fragments():
    from io import BytesIO

    from pymp4.parser import MP4

    init = build_init_segment(
        stream_type="text", fourcc="TTML", codec_private_data="", duration=6_000_000_000, language="eng"
    )
    data = init + text_fragment(1, "00:00:01.000", "00:00:02.000", "first", sdi=1)
    types = [b.type for b in MP4.parse_stream(BytesIO(data))]
    assert types == [b"ftyp", b"moov", b"moof", b"mdat"]


def test_fttml_subtitle_parse_yields_cues():
    from unshackle.core.tracks.subtitle import Subtitle

    init = build_init_segment(
        stream_type="text", fourcc="TTML", codec_private_data="", duration=6_000_000_000, language="eng"
    )
    data = (
        init
        + text_fragment(1, "00:00:01.000", "00:00:02.000", "first", sdi=1)
        + text_fragment(2, "00:00:03.000", "00:00:04.000", "second", sdi=2)
    )
    caption_set = Subtitle.parse(data, Subtitle.Codec.fTTML)
    cues = [c for lang in caption_set.get_languages() for c in caption_set.get_captions(lang)]
    assert len(cues) == 2
    assert [c.get_text() for c in cues] == ["first", "second"]


@pytest.mark.parametrize(
    "fourcc,expected",
    [("HEV1", b"hev1"), ("hev1", b"hev1"), ("HVC1", b"hvc1"), ("hvc1", b"hvc1"), ("HEVC", b"hvc1"), ("H265", b"hvc1")],
)
def test_hevc_sample_entry_honours_manifest_fourcc(fourcc, expected):
    init = build_init_segment(
        stream_type="video", fourcc=fourcc, codec_private_data=VIDEO_HEVC_CPD, width=3840, height=2160
    )
    offset = find_stsd(init)
    assert init[offset + 20 : offset + 24] == expected


def test_encrypted_hev1_frma_carries_the_same_fourcc():
    init = build_init_segment(
        stream_type="video", fourcc="HEV1", codec_private_data=VIDEO_HEVC_CPD, kid=KID, width=3840, height=2160
    )
    assert b"encv" in init
    assert init[init.index(b"frma") + 4 : init.index(b"frma") + 8] == b"hev1"


def test_dolby_vision_sample_entry_carries_dvcc():
    init = build_init_segment(
        stream_type="video", fourcc="dvhe", codec_private_data=VIDEO_HEVC_CPD, width=3840, height=2160
    )
    offset = find_stsd(init)
    assert init[offset + 20 : offset + 24] == b"dvh1"

    entry_start, entry_end = sample_entry_bounds(init)
    assert entry_start <= init.index(b"dvcC") < entry_end

    payload = init[init.index(b"dvcC") + 4 :][:24]
    assert payload[0] == 1 and payload[1] == 0
    flags = struct.unpack(">H", payload[2:4])[0]
    assert flags >> 9 == 5
    assert (flags >> 3) & 0x3F == 6
    assert flags & 0b111 == 0b101


@pytest.mark.parametrize(
    "cpd,expected_box,expected_profile,expected_compat,expected_entry",
    [
        (VIDEO_HEVC_DV_CPD, b"dvcC", 5, 0, b"dvh1"),
        (VIDEO_HEVC_PQ_CPD, b"dvvC", 8, 1, b"hvc1"),
        (VIDEO_HEVC_HLG_CPD, b"dvvC", 8, 4, b"hvc1"),
    ],
)
def test_dolby_vision_profile_follows_the_base_layer_colour(
    cpd, expected_box, expected_profile, expected_compat, expected_entry
):
    init = build_init_segment(stream_type="video", fourcc="DVH1", codec_private_data=cpd, width=3840, height=2160)
    assert expected_box in init
    entry_start, entry_end = sample_entry_bounds(init)
    assert entry_start <= init.index(expected_box) < entry_end
    payload = init[init.index(expected_box) + 4 :][:24]
    assert struct.unpack(">H", payload[2:4])[0] >> 9 == expected_profile
    assert struct.unpack(">I", payload[4:8])[0] >> 28 == expected_compat
    offset = find_stsd(init)
    assert init[offset + 20 : offset + 24] == expected_entry


def test_dolby_vision_profile_8_dvhe_takes_hev1_and_frma_follows():
    init = build_init_segment(
        stream_type="video", fourcc="dvhe", codec_private_data=VIDEO_HEVC_PQ_CPD, width=3840, height=2160
    )
    offset = find_stsd(init)
    assert init[offset + 20 : offset + 24] == b"hev1"
    assert b"dvvC" in init

    init = build_init_segment(
        stream_type="video", fourcc="dvhe", codec_private_data=VIDEO_HEVC_PQ_CPD, width=3840, height=2160, kid=KID
    )
    assert b"encv" in init and b"dvvC" in init
    assert init[init.index(b"frma") + 4 : init.index(b"frma") + 8] == b"hev1"
    entry_start, entry_end = sample_entry_bounds(init)
    assert entry_start <= init.index(b"dvvC") < entry_end


def test_encrypted_dolby_vision_wraps_encv_with_dvcc_before_sinf():
    init = build_init_segment(
        stream_type="video", fourcc="DVH1", codec_private_data=VIDEO_HEVC_DV_CPD, width=3840, height=2160, kid=KID
    )
    assert b"encv" in init and b"tenc" in init
    assert init[init.index(b"frma") + 4 : init.index(b"frma") + 8] == b"dvh1"
    entry_start, entry_end = sample_entry_bounds(init)
    dvcc_at = init.index(b"dvcC")
    assert entry_start <= dvcc_at < entry_end
    assert dvcc_at < init.index(b"sinf") < entry_end


@pytest.mark.parametrize(
    "fps,expected_level",
    [
        (24.0, 6),
        (30.0, 7),
        (48.0, 8),
        (60.0, 9),
        (120.0, 10),
        (None, 7),
    ],
)
def test_dolby_vision_level_scales_with_frame_rate(fps, expected_level):
    payload = build_dvcc(3840, 2160, fps, (2, 2, 2))[8:]
    flags = struct.unpack(">H", payload[2:4])[0]
    assert (flags >> 3) & 0x3F == expected_level


def test_dolby_vision_ftyp_carries_dby1_brand():
    dv_init = build_init_segment(
        stream_type="video", fourcc="DVH1", codec_private_data=VIDEO_HEVC_DV_CPD, width=3840, height=2160
    )
    ftyp_size = struct.unpack(">I", dv_init[:4])[0]
    assert b"dby1" in dv_init[:ftyp_size]

    hevc_init = build_init_segment(
        stream_type="video", fourcc="HVC1", codec_private_data=VIDEO_HEVC_CPD, width=3840, height=2160
    )
    assert b"dby1" not in hevc_init


def test_hevc_cpd_without_vps_builds_init():
    nals = split_nal_units(bytes.fromhex(VIDEO_HEVC_PQ_CPD))
    cpd = b"".join(b"\x00\x00\x00\x01" + n for n in nals if (n[0] >> 1) & 0x3F != 32).hex()

    init = build_init_segment(stream_type="video", fourcc="HVC1", codec_private_data=cpd, width=1920, height=1080)
    assert b"hvcC" in init

    init = build_init_segment(
        stream_type="video", fourcc="HVC1", codec_private_data=cpd, width=1920, height=1080, kid=KID
    )
    assert b"sinf" in init and b"tenc" in init

    no_sps = b"".join(b"\x00\x00\x00\x01" + n for n in nals if (n[0] >> 1) & 0x3F not in (32, 33)).hex()
    with pytest.raises(ValueError):
        build_init_segment(stream_type="video", fourcc="HVC1", codec_private_data=no_sps, width=1920, height=1080)


@pytest.mark.parametrize(
    "width,height,expected_level",
    [
        (1920, 1080, 3),
        (3840, 2160, 6),
        (7680, 4320, 11),
        (3840, 600, 5),
    ],
)
def test_dolby_vision_level_needs_both_pixel_rate_and_width(width, height, expected_level):
    init = build_init_segment(
        stream_type="video", fourcc="DVH1", codec_private_data=VIDEO_HEVC_DV_CPD, width=width, height=height
    )
    payload = init[init.index(b"dvcC") + 4 :][:24]
    assert (struct.unpack(">H", payload[2:4])[0] >> 3) & 0x3F == expected_level
