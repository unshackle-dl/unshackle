"""
Synthesize an ISO-BMFF initialization segment (ftyp + moov) for ISM / Smooth
Streaming tracks.

Smooth Streaming fragments are bare ``moof`` + ``mdat`` pairs; the server never
sends a ``moov``. The init box must be reconstructed from the manifest's
``CodecPrivateData`` (and, for protected content, the track KID) before a muxer
or decryptor such as shaka-packager can parse the stream. Ported from yt-dlp's
``write_piff_header`` and N_m3u8DL-RE's ``MSSMoovProcessor`` with HEVC, Dolby
Vision, EC-3, TTML and CENC (PIFF) support.
"""

from __future__ import annotations

import binascii
import struct
from typing import Iterator, Optional

# Big-endian field packers (named for the bit widths they encode).
u8 = struct.Struct(">B")
u16 = struct.Struct(">H")
u32 = struct.Struct(">I")
u64 = struct.Struct(">Q")
s16 = struct.Struct(">h")
s88 = struct.Struct(">bx")  # 8.8 fixed-point
s1616 = struct.Struct(">hxx")  # 16.16 fixed-point
u1616 = struct.Struct(">Hxx")
s32 = struct.Struct(">i")

# 3x3 transformation matrix (identity), as stored in tkhd/mvhd.
UNITY_MATRIX = (
    s32.pack(0x10000) + s32.pack(0) * 3
    + s32.pack(0) + s32.pack(0x10000) + s32.pack(0) * 2
    + s32.pack(0) * 2 + s32.pack(0x40000000)
)

TRACK_ENABLED = 0x1
TRACK_IN_MOVIE = 0x2
TRACK_IN_PREVIEW = 0x4
SELF_CONTAINED = 0x1

# Fixed creation/modification time — deterministic output (no wall clock).
EPOCH = 0

NAL_START_CODE = b"\x00\x00\x00\x01"

# WAVEFORMATEXTENSIBLE SubFormat GUID for Dolby Digital Plus, as serialized
# (little-endian) inside Smooth EC-3 CodecPrivateData.
DOLBY_DIGITAL_PLUS_GUID = bytes.fromhex("AF87FBA7022DFB42A4D405CD93843BDD")

# PIFF SampleEncryptionBox usertype (the pre-CENC 'senc' carried as a uuid box).
PIFF_SENC_UUID = bytes.fromhex("A2394F525A9B4F14A2446C427C648DF4")

TTML_NAMESPACE = b"http://www.w3.org/ns/ttml\0"

# ISO/IEC 14496-3 samplingFrequencyIndex table for AudioSpecificConfig.
AAC_SAMPLING_FREQUENCY_INDEX = {
    96000: 0x0,
    88200: 0x1,
    64000: 0x2,
    48000: 0x3,
    44100: 0x4,
    32000: 0x5,
    24000: 0x6,
    22050: 0x7,
    16000: 0x8,
    12000: 0x9,
    11025: 0xA,
    8000: 0xB,
    7350: 0xC,
}


def box(box_type: bytes, payload: bytes) -> bytes:
    """Wrap payload in a basic ISO-BMFF box (size + fourcc + payload)."""
    return u32.pack(8 + len(payload)) + box_type + payload


def full_box(box_type: bytes, version: int, flags: int, payload: bytes) -> bytes:
    """Wrap payload in a FullBox (adds 1-byte version + 3-byte flags)."""
    return box(box_type, u8.pack(version) + u32.pack(flags)[1:] + payload)


def split_nal_units(codec_private_data: bytes) -> list[bytes]:
    """Split CodecPrivateData into its NAL units (drops the start codes)."""
    units = [u for u in codec_private_data.split(NAL_START_CODE) if u]
    return units


def remove_emulation_prevention(data: bytes) -> bytes:
    """Strip H.26x emulation-prevention bytes (the 0x03 in any 00 00 03 run).

    The byte after a consumed escape is data — even another 0x03 — so the scan
    must skip past it rather than re-examine (a naive trailing-window check
    over-strips consecutive escapes and shifts every later bit position).
    """
    out = bytearray()
    i = 0
    while i < len(data):
        if i + 2 < len(data) and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out += b"\x00\x00"
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


class BitReader:
    """MSB-first bit reader with the exp-Golomb decode H.26x headers need."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read_bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            byte = self.data[self.pos >> 3]
            value = (value << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return value

    def read_ue(self) -> int:
        zeros = 0
        while self.read_bits(1) == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError("Invalid exp-Golomb code")
        return (1 << zeros) - 1 + (self.read_bits(zeros) if zeros else 0)

    def read_se(self) -> int:
        # se(v): 0,1,2,3,... -> 0,1,-1,2,...
        value = self.read_ue()
        return (value + 1) >> 1 if value & 1 else -(value >> 1)


def read_hevc_sps_to_bit_depth(r: BitReader) -> tuple[int, int, int, int]:
    """Advance reader through bit_depth_chroma_minus8; returns
    (chroma_format_idc, bit_depth_luma, bit_depth_chroma, max_sub_layers_minus1)."""
    r.read_bits(16)  # NAL unit header
    r.read_bits(4)  # sps_video_parameter_set_id
    max_sub_layers_minus1 = r.read_bits(3)
    r.read_bits(1)  # sps_temporal_id_nesting_flag
    r.read_bits(96)  # general profile_tier_level (12 bytes)
    profile_present = []
    level_present = []
    for _ in range(max_sub_layers_minus1):
        profile_present.append(r.read_bits(1))
        level_present.append(r.read_bits(1))
    if max_sub_layers_minus1 > 0:
        r.read_bits((8 - max_sub_layers_minus1) * 2)  # reserved_zero_2bits
    for i in range(max_sub_layers_minus1):
        if profile_present[i]:
            r.read_bits(88)  # sub_layer profile_tier
        if level_present[i]:
            r.read_bits(8)  # sub_layer_level_idc
    r.read_ue()  # sps_seq_parameter_set_id
    chroma_format_idc = r.read_ue()
    if chroma_format_idc == 3:
        r.read_bits(1)  # separate_colour_plane_flag
    r.read_ue()  # pic_width_in_luma_samples
    r.read_ue()  # pic_height_in_luma_samples
    if r.read_bits(1):  # conformance_window_flag
        for _ in range(4):
            r.read_ue()
    bit_depth_luma_minus8 = r.read_ue()
    bit_depth_chroma_minus8 = r.read_ue()
    return chroma_format_idc, bit_depth_luma_minus8, bit_depth_chroma_minus8, max_sub_layers_minus1


def parse_hevc_sps_format(sps_rbsp: bytes) -> tuple[int, int, int]:
    """
    Parse (chroma_format_idc, bit_depth_luma_minus8, bit_depth_chroma_minus8)
    from a de-emulated HEVC SPS RBSP (including its 2-byte NAL header).
    """
    r = BitReader(sps_rbsp)
    chroma_format_idc, bit_depth_luma_minus8, bit_depth_chroma_minus8, _ = read_hevc_sps_to_bit_depth(r)
    return chroma_format_idc, bit_depth_luma_minus8, bit_depth_chroma_minus8


def skip_hevc_scaling_list_data(r: BitReader) -> None:
    """Skip an HEVC scaling_list_data() syntax structure (H.265 7.3.4)."""
    for size_id in range(4):
        matrix_id = 0
        while matrix_id < 6:
            if not r.read_bits(1):  # scaling_list_pred_mode_flag
                r.read_ue()  # scaling_list_pred_matrix_id_delta
            else:
                coef_num = min(64, 1 << (4 + (size_id << 1)))
                if size_id > 1:
                    r.read_se()  # scaling_list_dc_coef_minus8
                for _ in range(coef_num):
                    r.read_se()  # scaling_list_delta_coef
            matrix_id += 3 if size_id == 3 else 1


def skip_hevc_st_ref_pic_set(r: BitReader, idx: int, num_delta_pocs: list[int]) -> int:
    """Skip one st_ref_pic_set() (H.265 7.3.7); returns its NumDeltaPocs."""
    if idx and r.read_bits(1):  # inter_ref_pic_set_prediction_flag
        r.read_bits(1)  # delta_rps_sign
        r.read_ue()  # abs_delta_rps_minus1
        count = 0
        for _ in range(num_delta_pocs[idx - 1] + 1):
            used_by_curr_pic = r.read_bits(1)
            use_delta = 1 if used_by_curr_pic else r.read_bits(1)
            if used_by_curr_pic or use_delta:
                count += 1
        return count
    num_negative = r.read_ue()
    num_positive = r.read_ue()
    for _ in range(num_negative + num_positive):
        r.read_ue()  # delta_poc_sX_minus1
        r.read_bits(1)  # used_by_curr_pic_sX_flag
    return num_negative + num_positive


def parse_hevc_sps_colour(sps_rbsp: bytes) -> Optional[tuple[int, int, int]]:
    """VUI (colour_primaries, transfer_characteristics, matrix_coeffs) from a
    de-emulated HEVC SPS, or None when no colour description is present."""
    r = BitReader(sps_rbsp)
    _, _, _, max_sub_layers_minus1 = read_hevc_sps_to_bit_depth(r)
    log2_max_poc_lsb_minus4 = r.read_ue()
    sub_layer_ordering_info = r.read_bits(1)
    for _ in range(max_sub_layers_minus1 + 1 if sub_layer_ordering_info else 1):
        r.read_ue()  # sps_max_dec_pic_buffering_minus1
        r.read_ue()  # sps_max_num_reorder_pics
        r.read_ue()  # sps_max_latency_increase_plus1
    for _ in range(6):  # luma coding/transform block sizes + transform hierarchy depths
        r.read_ue()
    if r.read_bits(1) and r.read_bits(1):  # scaling_list_enabled + sps_scaling_list_data_present
        skip_hevc_scaling_list_data(r)
    r.read_bits(2)  # amp_enabled_flag + sample_adaptive_offset_enabled_flag
    if r.read_bits(1):  # pcm_enabled_flag
        r.read_bits(8)  # pcm sample bit depths (4 + 4)
        r.read_ue()  # log2_min_pcm_luma_coding_block_size_minus3
        r.read_ue()  # log2_diff_max_min_pcm_luma_coding_block_size
        r.read_bits(1)  # pcm_loop_filter_disabled_flag
    num_delta_pocs: list[int] = []
    for idx in range(r.read_ue()):  # num_short_term_ref_pic_sets
        num_delta_pocs.append(skip_hevc_st_ref_pic_set(r, idx, num_delta_pocs))
    if r.read_bits(1):  # long_term_ref_pics_present_flag
        for _ in range(r.read_ue()):  # num_long_term_ref_pics_sps
            r.read_bits(log2_max_poc_lsb_minus4 + 4)  # lt_ref_pic_poc_lsb_sps
            r.read_bits(1)  # used_by_curr_pic_lt_sps_flag
    r.read_bits(2)  # sps_temporal_mvp_enabled + strong_intra_smoothing_enabled
    if not r.read_bits(1):  # vui_parameters_present_flag
        return None
    if r.read_bits(1):  # aspect_ratio_info_present_flag
        if r.read_bits(8) == 255:  # aspect_ratio_idc == EXTENDED_SAR
            r.read_bits(32)  # sar_width + sar_height
    if r.read_bits(1):  # overscan_info_present_flag
        r.read_bits(1)  # overscan_appropriate_flag
    if not r.read_bits(1):  # video_signal_type_present_flag
        return None
    r.read_bits(3)  # video_format
    r.read_bits(1)  # video_full_range_flag
    if not r.read_bits(1):  # colour_description_present_flag
        return None
    return r.read_bits(8), r.read_bits(8), r.read_bits(8)


HEVC_FOURCCS = frozenset(("HVC1", "HEV1", "HEVC", "H265", "DVHE", "DVH1"))


def parse_codec_private_data_colour(fourcc: str, codec_private_data: bytes) -> Optional[tuple[int, int, int]]:
    """SPS VUI colour triple from HEVC CodecPrivateData; None when the codec
    is unsupported, no colour description, or malformed data."""
    if (fourcc or "").upper() not in HEVC_FOURCCS:
        return None
    try:
        nals = split_nal_units(codec_private_data)
        sps = next((n for n in nals if (n[0] >> 1) & 0x3F == 33), None)
        if sps is None:
            return None
        return parse_hevc_sps_colour(remove_emulation_prevention(sps))
    except (IndexError, ValueError):
        return None


def iter_boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, Optional[bytes], int, int]]:
    """Yield (type, uuid_usertype, payload_start, box_end) for each child box."""
    offset = start
    while offset + 8 <= end:
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        box_type = data[offset + 4 : offset + 8]
        header = 8
        if size == 1:
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            header = 16
        if size == 0:
            size = end - offset
        if size < 8:  # corrupt box header; stop rather than loop forever
            return
        usertype = None
        if box_type == b"uuid" and offset + header + 16 <= end:
            usertype = data[offset + header : offset + header + 16]
            header += 16
        yield box_type, usertype, offset + header, offset + size
        offset += size


def find_box(data: bytes, start: int, end: int, target: bytes) -> Optional[tuple[int, int]]:
    """Find the first child box of the given type; return (payload_start, end)."""
    for box_type, _, body, box_end in iter_boxes(data, start, end):
        if box_type == target:
            return body, box_end
    return None


def read_track_id(fragment: bytes) -> Optional[int]:
    """Read the track_ID from a fragment's moof/traf/tfhd box, if present.

    Smooth fragments declare their own track_ID; the synthesized moov must use
    the same value or the muxer cannot associate samples with the track. The
    track_ID sits before any tfhd optional fields, so the flags don't matter.
    """
    moof = find_box(fragment, 0, len(fragment), b"moof")
    if not moof:
        return None
    traf = find_box(fragment, *moof, b"traf")
    if not traf:
        return None
    tfhd = find_box(fragment, *traf, b"tfhd")
    if not tfhd:
        return None
    body, _ = tfhd
    if body + 8 > len(fragment):  # truncated tfhd
        return None
    # tfhd payload: version(1) + flags(3) + track_ID(4)
    return struct.unpack(">I", fragment[body + 4 : body + 8])[0]


def read_per_sample_iv_size(fragment: bytes) -> Optional[int]:
    """
    Derive the per-sample IV size (8 or 16) from a fragment's sample-encryption
    metadata, for the synthesized tenc default_Per_Sample_IV_Size.

    Checks, in order: the PIFF 'senc' uuid override flag (explicit IV size),
    the senc payload length (sample_count vs IV/subsample entries), and the
    saiz default_sample_info_size (only unambiguous without subsamples).
    """
    moof = find_box(fragment, 0, len(fragment), b"moof")
    if not moof:
        return None
    traf = find_box(fragment, *moof, b"traf")
    if not traf:
        return None

    senc: Optional[tuple[int, int]] = None
    saiz_default: Optional[int] = None
    senc_has_subsamples = False
    for box_type, usertype, body, box_end in iter_boxes(fragment, *traf):
        if box_type == b"senc" or (box_type == b"uuid" and usertype == PIFF_SENC_UUID):
            senc = (body, box_end)
        elif box_type == b"saiz":
            flags = int.from_bytes(fragment[body + 1 : body + 4], "big")
            pos = body + 4 + (8 if flags & 0x1 else 0)  # skip aux_info_type fields
            if pos < box_end:
                saiz_default = fragment[pos]

    if senc:
        body, box_end = senc
        flags = int.from_bytes(fragment[body + 1 : body + 4], "big")
        senc_has_subsamples = bool(flags & 0x2)
        pos = body + 4
        if flags & 0x1:  # PIFF override: AlgorithmID(3) + IV_size(1) + KID(16)
            return fragment[pos + 3]
        if pos + 4 <= box_end:
            sample_count = struct.unpack(">I", fragment[pos : pos + 4])[0]
            pos += 4
            if sample_count:
                if not senc_has_subsamples:
                    size, rem = divmod(box_end - pos, sample_count)
                    if rem == 0 and size in (8, 16):
                        return size
                else:
                    # Walk the entries with each candidate IV size; the one that
                    # lands exactly on the box end is correct.
                    for iv_size in (8, 16):
                        cursor = pos
                        for _ in range(sample_count):
                            cursor += iv_size
                            if cursor + 2 > box_end:
                                cursor = -1
                                break
                            entries = struct.unpack(">H", fragment[cursor : cursor + 2])[0]
                            cursor += 2 + 6 * entries
                            if cursor > box_end:
                                cursor = -1
                                break
                        if cursor == box_end:
                            return iv_size

    if not senc_has_subsamples and saiz_default in (8, 16):
        return saiz_default
    return None


def build_avcc(codec_private_data: bytes, nal_length_size: int = 4) -> bytes:
    """Build an avcC (AVC decoder config) box from SPS+PPS CodecPrivateData."""
    nals = split_nal_units(codec_private_data)
    # Pick parameter sets by H.264 NAL type (low 5 bits): 7 = SPS, 8 = PPS.
    # Manifests do not guarantee SPS-first ordering.
    sps = next((n for n in nals if n[0] & 0x1F == 7), None)
    pps = next((n for n in nals if n[0] & 0x1F == 8), None)
    if not sps or not pps:
        raise ValueError("AVC CodecPrivateData must contain SPS and PPS NAL units")
    payload = u8.pack(1)  # configuration version
    payload += sps[1:4]  # profile / compat / level (from SPS NAL body)
    payload += u8.pack(0xFC | (nal_length_size - 1))  # reserved + length size minus one
    payload += u8.pack(0xE0 | 1)  # reserved + number of SPS (1)
    payload += u16.pack(len(sps)) + sps
    payload += u8.pack(1)  # number of PPS
    payload += u16.pack(len(pps)) + pps
    return box(b"avcC", payload)


def build_hvcc(codec_private_data: bytes, nal_length_size: int = 4) -> bytes:
    """
    Build an hvcC (HEVC decoder config) box from VPS+SPS+PPS CodecPrivateData.

    Profile/tier/level bytes are lifted from the SPS profile_tier_level; chroma
    format and bit depths are parsed from the SPS so 10-bit/HDR streams signal
    correctly (falls back to 8-bit 4:2:0 on malformed SPS data).
    """
    nals = split_nal_units(codec_private_data)
    if len(nals) < 3:
        raise ValueError("HEVC CodecPrivateData must contain VPS, SPS and PPS NAL units")

    # Group NAL units by type (HEVC NAL type = (first byte >> 1) & 0x3F).
    by_type: dict[int, list[bytes]] = {}
    for nal in nals:
        nal_type = (nal[0] >> 1) & 0x3F
        by_type.setdefault(nal_type, []).append(nal)

    sps = by_type.get(33, [b""])[0]
    # profile_tier_level must be read from the de-emulated SPS RBSP, after the
    # 2-byte NAL header + 1 byte (sps_video_parameter_set_id(4) +
    # sps_max_sub_layers_minus1(3) + sps_temporal_id_nesting_flag(1)). PTL is 12
    # bytes: profile byte(1) + compat flags(4) + constraint flags(6) + level(1).
    sps_rbsp = remove_emulation_prevention(sps)
    ptl = sps_rbsp[3:15] if len(sps_rbsp) >= 15 else b"\x00" * 12
    general_profile_space_tier_profile = ptl[0:1] or b"\x00"
    general_profile_compat = ptl[1:5].ljust(4, b"\x00")
    general_constraint = ptl[5:11].ljust(6, b"\x00")
    general_level_idc = ptl[11:12] or b"\x00"

    try:
        chroma_format_idc, bit_depth_luma_minus8, bit_depth_chroma_minus8 = parse_hevc_sps_format(sps_rbsp)
    except (IndexError, ValueError):
        chroma_format_idc, bit_depth_luma_minus8, bit_depth_chroma_minus8 = 1, 0, 0

    payload = u8.pack(1)  # configurationVersion
    payload += general_profile_space_tier_profile
    payload += general_profile_compat
    payload += general_constraint
    payload += general_level_idc
    payload += u16.pack(0xF000)  # reserved(4) + min_spatial_segmentation_idc(12)
    payload += u8.pack(0xFC)  # reserved(6) + parallelismType(2)
    payload += u8.pack(0xFC | (chroma_format_idc & 0x03))  # reserved(6) + chromaFormat(2)
    payload += u8.pack(0xF8 | (bit_depth_luma_minus8 & 0x07))  # reserved(5) + bitDepthLumaMinus8(3)
    payload += u8.pack(0xF8 | (bit_depth_chroma_minus8 & 0x07))  # reserved(5) + bitDepthChromaMinus8(3)
    payload += u16.pack(0)  # avgFrameRate
    # constantFrameRate(2)+numTemporalLayers(3)+temporalIdNested(1)+lengthSizeMinusOne(2)
    payload += u8.pack((nal_length_size - 1) & 0x03)

    arrays = bytearray()
    num_arrays = 0
    for nal_type in (32, 33, 34):  # VPS, SPS, PPS
        units = by_type.get(nal_type)
        if not units:
            continue
        num_arrays += 1
        arrays += u8.pack(0x80 | nal_type)  # array_completeness(1)+reserved(1)+NAL type(6)
        arrays += u16.pack(len(units))
        for unit in units:
            arrays += u16.pack(len(unit)) + unit
    payload += u8.pack(num_arrays) + bytes(arrays)
    return box(b"hvcC", payload)


def build_esds(codec_private_data: bytes) -> bytes:
    """Build an esds box wrapping the AAC AudioSpecificConfig."""
    asc = codec_private_data
    # DecoderSpecificInfo (tag 0x05)
    dsi = u8.pack(0x05) + u8.pack(len(asc)) + asc
    # DecoderConfigDescriptor (tag 0x04): objectType=0x40 (AAC), stream type audio
    dcd = (
        u8.pack(0x40)  # object type indication = MPEG-4 AAC
        + u8.pack(0x15)  # stream type (audio) << 2 | upstream | reserved
        + b"\x00\x00\x00"  # buffer size
        + u32.pack(0)  # max bitrate
        + u32.pack(0)  # avg bitrate
        + dsi
    )
    dcd_box = u8.pack(0x04) + u8.pack(len(dcd)) + dcd
    # SLConfigDescriptor (tag 0x06)
    sl = u8.pack(0x06) + u8.pack(1) + u8.pack(0x02)
    # ES_Descriptor (tag 0x03)
    es = u8.pack(0x03) + u8.pack(len(dcd_box) + len(sl) + 3) + u16.pack(0) + u8.pack(0) + dcd_box + sl
    return full_box(b"esds", 0, 0, es)


def build_dec3(codec_private_data: bytes) -> Optional[bytes]:
    """Build a dec3 (EC-3 specific) box from Smooth EC-3 CodecPrivateData.

    Smooth EC-3 CodecPrivateData ([MS-SSTR] AudioTag 65534) serializes a
    WAVEFORMATEXTENSIBLE — sometimes the full structure, sometimes only its
    extension (samples-per-block + channel mask + DD+ SubFormat GUID) — with
    the raw dec3 payload (ETSI TS 102 366 F.6) after the GUID. Returns None
    when the GUID is absent — decoders still sync from EC-3 frames in mdat.
    """
    guid_at = codec_private_data.find(DOLBY_DIGITAL_PLUS_GUID)
    if guid_at != -1 and len(codec_private_data) > guid_at + 16:
        return box(b"dec3", codec_private_data[guid_at + 16 :])
    return None


def synthesize_aac_codec_private_data(fourcc: str, sampling_rate: int, channels: int) -> bytes:
    """Generate the AAC AudioSpecificConfig when the manifest omits it.

    AACL -> 2-byte AAC-LC config; AACH -> 4-byte HE-AAC (SBR, AOT 5) config
    with the extension sampling frequency at twice the core rate.
    """
    freq = AAC_SAMPLING_FREQUENCY_INDEX.get(sampling_rate, 0x0)
    if fourcc == "AACH":
        ext_freq = AAC_SAMPLING_FREQUENCY_INDEX.get(sampling_rate * 2, 0x0)
        return bytes(
            (
                (0x05 << 3) | (freq >> 1),
                ((freq & 0x01) << 7) | (channels << 3) | (ext_freq >> 1),
                ((ext_freq & 0x01) << 7) | (0x02 << 2),  # core object type = AAC LC
                0x00,  # alignment bits
            )
        )
    return bytes(((0x02 << 3) | (freq >> 1), ((freq & 0x01) << 7) | (channels << 3)))


def build_sinf(
    original_format: bytes,
    kid: bytes,
    iv_size: int = 8,
    constant_iv: Optional[bytes] = None,
) -> bytes:
    """Build a sinf protection box (frma + schm cenc + schi/tenc) for CENC.

    iv_size is the tenc default_Per_Sample_IV_Size (8 or 16). When constant_iv
    is given, the per-sample IV size is 0 and the constant IV is appended per
    ISO/IEC 23001-7 (cbcs-style constant-IV form).
    """
    frma = box(b"frma", original_format)
    schm = full_box(b"schm", 0, 0, b"cenc" + u32.pack(0x00010000))
    tenc_payload = (
        u8.pack(0)  # reserved
        + u8.pack(0)  # default_crypt_byte_block / skip_byte_block (cenc)
        + u8.pack(1)  # default_isProtected
        + u8.pack(0 if constant_iv else iv_size)  # default_Per_Sample_IV_Size
        + kid  # default_KID (16 bytes)
    )
    if constant_iv:
        tenc_payload += u8.pack(len(constant_iv)) + constant_iv
    schi = box(b"schi", full_box(b"tenc", 0, 0, tenc_payload))
    return box(b"sinf", frma + schm + schi)


def build_init_segment(
    *,
    stream_type: str,
    fourcc: str,
    codec_private_data: str,
    timescale: int = 10000000,
    duration: int = 0,
    language: str = "und",
    width: int = 0,
    height: int = 0,
    channels: int = 2,
    bits_per_sample: int = 16,
    sampling_rate: int = 48000,
    track_id: int = 1,
    nal_length_size: int = 4,
    kid: Optional[bytes] = None,
    iv_size: int = 8,
    constant_iv: Optional[bytes] = None,
) -> bytes:
    """
    Build a complete ftyp + moov initialization segment.

    stream_type: "video" | "audio" | "text".
    fourcc: Smooth FourCC ("H264"/"AVC1"/"DAVC", "HVC1"/"HEV1", "DVHE"/"DVH1",
            "AACL"/"AACH", "EC-3", "TTML").
    codec_private_data: hex string from the manifest QualityLevel.
    nal_length_size: manifest NALUnitLengthField (bytes per NAL length prefix).
    kid: 16-byte default key id; when set, the sample entry is wrapped for CENC.
    iv_size / constant_iv: tenc IV form (see build_sinf).
    """
    if stream_type not in ("video", "audio", "text"):
        raise ValueError(f"Unsupported stream type: {stream_type}")
    fourcc = (fourcc or "").upper()
    cpd = binascii.unhexlify(codec_private_data) if codec_private_data else b""
    encrypted = kid is not None
    # mdhd packs exactly three a-z letters; anything else (2-letter tags,
    # uppercase) would underflow the 5-bit fields, so fall back to "und".
    lang = (language or "").lower()
    if len(lang) != 3 or not all("a" <= c <= "z" for c in lang):
        lang = "und"

    # --- ftyp ---
    ftyp = box(b"ftyp", b"isml" + u32.pack(1) + b"iso5" + b"iso6" + b"piff" + b"msdh")

    # --- mvhd ---
    mvhd = full_box(
        b"mvhd", 1, 0,
        u64.pack(EPOCH) + u64.pack(EPOCH) + u32.pack(timescale) + u64.pack(duration)
        + s1616.pack(1) + s88.pack(1) + u16.pack(0) + u32.pack(0) * 2
        + UNITY_MATRIX + u32.pack(0) * 6 + u32.pack(0xFFFFFFFF),
    )

    # --- tkhd ---
    tkhd = full_box(
        b"tkhd", 1, TRACK_ENABLED | TRACK_IN_MOVIE | TRACK_IN_PREVIEW,
        u64.pack(EPOCH) + u64.pack(EPOCH) + u32.pack(track_id) + u32.pack(0)
        + u64.pack(duration) + u32.pack(0) * 2 + s16.pack(0) + s16.pack(0)
        + s88.pack(1 if stream_type == "audio" else 0) + u16.pack(0) + UNITY_MATRIX
        + u1616.pack(width) + u1616.pack(height),
    )

    # --- mdhd + hdlr ---
    packed_lang = ((ord(lang[0]) - 0x60) << 10) | ((ord(lang[1]) - 0x60) << 5) | (ord(lang[2]) - 0x60)
    mdhd = full_box(
        b"mdhd", 1, 0,
        u64.pack(EPOCH) + u64.pack(EPOCH) + u32.pack(timescale) + u64.pack(duration)
        + u16.pack(packed_lang) + u16.pack(0),
    )
    if stream_type == "audio":
        hdlr = full_box(b"hdlr", 0, 0, u32.pack(0) + b"soun" + u32.pack(0) * 3 + b"SoundHandler\0")
        media_header = full_box(b"smhd", 0, 0, s88.pack(0) + u16.pack(0))
    elif stream_type == "text":
        hdlr = full_box(b"hdlr", 0, 0, u32.pack(0) + b"subt" + u32.pack(0) * 3 + b"SubtitleHandler\0")
        media_header = full_box(b"sthd", 0, 0, b"")
    else:
        hdlr = full_box(b"hdlr", 0, 0, u32.pack(0) + b"vide" + u32.pack(0) * 3 + b"VideoHandler\0")
        media_header = full_box(b"vmhd", 0, 1, u16.pack(0) + u16.pack(0) * 3)

    # --- dinf ---
    dref = full_box(b"dref", 0, 0, u32.pack(1) + full_box(b"url ", 0, SELF_CONTAINED, b""))
    dinf = box(b"dinf", dref)

    # --- stsd sample entry ---
    sample_entry_payload = u8.pack(0) * 6 + u16.pack(1)  # reserved + data reference index
    if stream_type == "video":
        sample_entry_payload += (
            u16.pack(0) + u16.pack(0) + u32.pack(0) * 3
            + u16.pack(width) + u16.pack(height)
            + u1616.pack(0x48) + u1616.pack(0x48) + u32.pack(0) + u16.pack(1)
            + u8.pack(0) * 32 + u16.pack(0x18) + s16.pack(-1)
        )
        if fourcc in ("H264", "AVC1", "DAVC"):
            config_box = build_avcc(cpd, nal_length_size)
            codec_fourcc = b"avc1"
        elif fourcc in ("HVC1", "HEV1", "HEVC", "H265"):
            config_box = build_hvcc(cpd, nal_length_size)
            codec_fourcc = b"hvc1"
        elif fourcc in ("DVHE", "DVH1"):
            # Dolby Vision over HEVC: same hvcC config, dvh1 sample entry.
            config_box = build_hvcc(cpd, nal_length_size)
            codec_fourcc = b"dvh1"
        else:
            raise NotImplementedError(f"Unsupported video FourCC: {fourcc}")
        sample_entry_payload += config_box
        if encrypted:
            sample_entry_payload += build_sinf(codec_fourcc, kid, iv_size, constant_iv)
            sample_entry_box = box(b"encv", sample_entry_payload)
        else:
            sample_entry_box = box(codec_fourcc, sample_entry_payload)
    elif stream_type == "audio":
        # samplerate is 16.16 fixed-point; rates above 65535 Hz are written as 0
        # (decoders read the real rate from the codec config), matching ffmpeg.
        sample_entry_payload += (
            u32.pack(0) * 2 + u16.pack(channels) + u16.pack(bits_per_sample)
            + u16.pack(0) + u16.pack(0) + u32.pack((sampling_rate if sampling_rate <= 0xFFFF else 0) << 16)
        )
        if fourcc in ("AACL", "AACH", "AAC"):
            if not cpd:
                cpd = synthesize_aac_codec_private_data(fourcc, sampling_rate, channels)
            sample_entry_payload += build_esds(cpd)
            codec_fourcc = b"mp4a"
        elif fourcc == "EC-3":
            dec3 = build_dec3(cpd)
            if dec3:
                sample_entry_payload += dec3
            codec_fourcc = b"ec-3"
        else:
            raise NotImplementedError(f"Unsupported audio FourCC: {fourcc}")
        if encrypted:
            sample_entry_payload += build_sinf(codec_fourcc, kid, iv_size, constant_iv)
            sample_entry_box = box(b"enca", sample_entry_payload)
        else:
            sample_entry_box = box(codec_fourcc, sample_entry_payload)
    else:  # text
        if fourcc in ("TTML", "STPP", "DFXP"):
            # XMLSubtitleSampleEntry: namespace + schema_location + aux mime types.
            sample_entry_payload += TTML_NAMESPACE + b"\0" + b"\0"
            sample_entry_box = box(b"stpp", sample_entry_payload)
        else:
            raise NotImplementedError(f"Unsupported text FourCC: {fourcc}")

    stsd = full_box(b"stsd", 0, 0, u32.pack(1) + sample_entry_box)

    # --- empty sample tables (fragmented: real samples live in moof/traf) ---
    stbl = box(
        b"stbl",
        stsd
        + full_box(b"stts", 0, 0, u32.pack(0))
        + full_box(b"stsc", 0, 0, u32.pack(0))
        + full_box(b"stsz", 0, 0, u32.pack(0) + u32.pack(0))
        + full_box(b"stco", 0, 0, u32.pack(0)),
    )

    minf = box(b"minf", media_header + dinf + stbl)
    mdia = box(b"mdia", mdhd + hdlr + minf)
    trak = box(b"trak", tkhd + mdia)

    # --- mvex (mehd + trex) signals a fragmented file ---
    mehd = full_box(b"mehd", 1, 0, u64.pack(duration))
    trex = full_box(
        b"trex", 0, 0,
        u32.pack(track_id) + u32.pack(1) + u32.pack(0) + u32.pack(0) + u32.pack(0),
    )
    mvex = box(b"mvex", mehd + trex)

    moov = box(b"moov", mvhd + trak + mvex)
    return ftyp + moov
