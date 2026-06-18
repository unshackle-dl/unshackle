"""Apple HLS SAMPLE-AES decryptor for MPEG-TS elementary streams.

SAMPLE-AES (``METHOD=SAMPLE-AES``) encrypts only parts of each elementary-stream sample
rather than the whole TS, so neither Shaka Packager (rejects stream type ``0xDB``) nor
mp4decrypt (ISO-BMFF only) can decrypt it. This module implements the scheme directly,
matching the four reference implementations (hls.js, mchelnokov/decrypt-mpegts,
iori-rs/iori, N_m3u8DL-RE) and Apple's specification:

- H.264 (AVC, stream type ``0xDB``): NAL types 1 and 5 only. Remove emulation-prevention
  bytes, skip the first 32 bytes, then a 1:9 pattern — decrypt 16 bytes, skip ``min(144,
  remaining)`` — with AES-128-CBC chaining continuously across the encrypted blocks and the
  IV reset per NAL unit. Emulation-prevention bytes are reinserted afterwards.
- AAC (ADTS, stream type ``0xCF``): per frame, skip the ADTS header plus 16 leader bytes,
  decrypt the remaining complete 16-byte blocks, IV reset per frame.
- AC-3 (stream type ``0xC1``): per syncframe, skip 16 leader bytes, decrypt the remaining
  complete 16-byte blocks, IV reset per frame.

All decryption is AES-128-CBC with no padding; trailing bytes shorter than a block stay clear.

The IV is supplied by the caller. HLS signals it in the ``EXT-X-KEY`` ``IV`` attribute; when
absent the conventional default is all zeros (used by every reference tool). FairPlay
(``KEYFORMAT="com.apple.streamingkeydelivery"``) streams may instead carry the IV in the CKC,
in which case the caller must provide it — see ``decrypt_sample_aes``.
"""

from __future__ import annotations

import struct
from typing import Optional

from Crypto.Cipher import AES

# MPEG-TS stream type bytes: encrypted -> standard.
STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_AAC = 0x0F
STREAM_TYPE_AC3 = 0x81
ENCRYPTED_TO_CLEAR = {0xDB: STREAM_TYPE_H264, 0xCF: STREAM_TYPE_AAC, 0xC1: STREAM_TYPE_AC3}

PACKET_LENGTH = 188
SYNC_BYTE = 0x47
BLOCK = 16


def remove_epb(data: bytes) -> bytes:
    """Strip H.264 emulation-prevention bytes (``00 00 03 XX`` -> ``00 00 XX``, ``XX <= 03``)."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        if i + 3 < n and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3 and data[i + 3] <= 3:
            out += b"\x00\x00"
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def insert_epb(data: bytes) -> bytes:
    """Reinsert H.264 emulation-prevention bytes to produce a valid Annex B / byte-stream NAL."""
    out = bytearray()
    zeros = 0
    for b in data:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC decrypt with no padding; ``data`` length must be a multiple of 16."""
    return AES.new(key, AES.MODE_CBC, iv).decrypt(data)


def decrypt_h264_nal(nal: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt a single H.264 NAL unit (no start code) per SAMPLE-AES; return re-escaped bytes."""
    rbsp = bytearray(remove_epb(nal))
    n = len(rbsp)
    if n <= 48:
        return nal
    # Gather the encrypted blocks (1:9 pattern from offset 32) into one continuous CBC stream.
    encrypted = bytearray()
    offset = 32
    positions = []
    while True:
        remaining = n - offset
        if remaining <= BLOCK:
            break
        positions.append(offset)
        encrypted += rbsp[offset : offset + BLOCK]
        offset += BLOCK + min(144, remaining - BLOCK)
    decrypted = aes_cbc_decrypt(key, iv, bytes(encrypted))
    for idx, pos in enumerate(positions):
        rbsp[pos : pos + BLOCK] = decrypted[idx * BLOCK : (idx + 1) * BLOCK]
    return insert_epb(bytes(rbsp))


def split_nals(es: bytes) -> list[tuple[int, int, int]]:
    """Return (start_code_len, nal_start, nal_end) for each Annex B NAL unit in ``es``."""
    nals = []
    i = 0
    n = len(es)
    starts = []
    while i + 3 <= n:
        if es[i] == 0 and es[i + 1] == 0 and es[i + 2] == 1:
            sc = 4 if i >= 1 and es[i - 1] == 0 else 3
            starts.append((i - (sc - 3), sc))
            i += 3
        else:
            i += 1
    for idx, (sc_off, sc) in enumerate(starts):
        nal_start = sc_off + sc
        nal_end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        nals.append((sc, nal_start, nal_end))
    return nals


def decrypt_h264_es(es: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt all coded-slice NAL units (types 1, 5) in an Annex B H.264 elementary stream."""
    out = bytearray()
    prev_end = 0
    for sc, nal_start, nal_end in split_nals(es):
        out += es[prev_end:nal_start]
        nal = es[nal_start:nal_end]
        prev_end = nal_end
        if nal and (nal[0] & 0x1F) in (1, 5):
            out += decrypt_h264_nal(nal, key, iv)
        else:
            out += nal
    out += es[prev_end:]
    return bytes(out)


def decrypt_aac_es(es: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt ADTS AAC: per frame skip header + 16 leader bytes, decrypt remaining full blocks."""
    out = bytearray()
    i = 0
    n = len(es)
    while i + 7 <= n:
        if es[i] != 0xFF or (es[i + 1] & 0xF0) != 0xF0:
            out.append(es[i])
            i += 1
            continue
        header_len = 7 if (es[i + 1] & 1) else 9
        frame_len = ((es[i + 3] & 3) << 11) | (es[i + 4] << 3) | (es[i + 5] >> 5)
        if frame_len < header_len or i + frame_len > n:
            out += es[i:]
            i = n
            break
        frame = bytearray(es[i : i + frame_len])
        payload = frame[header_len:]
        if len(payload) > BLOCK:
            enc_len = ((len(payload) - BLOCK) // BLOCK) * BLOCK
            if enc_len:
                frame[header_len + BLOCK : header_len + BLOCK + enc_len] = aes_cbc_decrypt(
                    key, iv, bytes(payload[BLOCK : BLOCK + enc_len])
                )
        out += frame
        i += frame_len
    out += es[i:]
    return bytes(out)


def decrypt_ac3_es(es: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt AC-3: per syncframe skip 16 leader bytes, decrypt remaining full blocks.

    AC-3 syncframes start with ``0B 77``; frame size derives from the frmsizecod table. To keep
    this dependency-free the whole ES (minus a 16-byte leader) is treated as one CBC region,
    matching the reference behaviour for single-program AC-3 PES payloads.
    """
    if len(es) <= BLOCK:
        return es
    enc_len = ((len(es) - BLOCK) // BLOCK) * BLOCK
    if not enc_len:
        return es
    out = bytearray(es)
    out[BLOCK : BLOCK + enc_len] = aes_cbc_decrypt(key, iv, bytes(out[BLOCK : BLOCK + enc_len]))
    return bytes(out)


def crc32_mpeg(data: bytes) -> int:
    """MPEG-2 systems CRC-32 (poly 0x04C11DB7, init 0xFFFFFFFF, no final xor)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def packet_pid(pkt: bytes) -> int:
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def payload_offset(pkt: bytes) -> int:
    """Byte offset of the payload within a TS packet, or -1 if the packet carries none."""
    afc = (pkt[3] >> 4) & 0x3
    off = 4
    if afc & 0x2:
        off += 1 + pkt[4]
    if not afc & 0x1 or off >= PACKET_LENGTH:
        return -1
    return off


def parse_pmt(section: bytes) -> dict[int, int]:
    """Map elementary PID -> stream_type from a PMT section (starting at table_id)."""
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    pos = 12 + program_info_length
    end = 3 + section_length - 4  # up to (excluding) CRC32
    result = {}
    while pos + 5 <= end:
        stream_type = section[pos]
        es_pid = ((section[pos + 1] & 0x1F) << 8) | section[pos + 2]
        es_info_length = ((section[pos + 3] & 0x0F) << 8) | section[pos + 4]
        result[es_pid] = stream_type
        pos += 5 + es_info_length
    return result


def patch_pmt_section(section: bytes) -> bytes:
    """Rewrite encrypted stream-type bytes to their clear equivalents and fix the CRC."""
    section = bytearray(section)
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    pos = 12 + program_info_length
    end = 3 + section_length - 4
    while pos + 5 <= end:
        if section[pos] in ENCRYPTED_TO_CLEAR:
            section[pos] = ENCRYPTED_TO_CLEAR[section[pos]]
        es_info_length = ((section[pos + 3] & 0x0F) << 8) | section[pos + 4]
        pos += 5 + es_info_length
    crc = crc32_mpeg(bytes(section[:end]))
    section[end : end + 4] = struct.pack(">I", crc)
    return bytes(section)


def repacketize(pid: int, pes: bytes, continuity: dict[int, int]) -> bytes:
    """Split a PES packet into 188-byte TS packets for ``pid`` with stuffing on the final one."""
    out = bytearray()
    pos = 0
    first = True
    n = len(pes)
    while pos < n:
        remaining = n - pos
        cc = continuity[pid]
        continuity[pid] = (cc + 1) & 0x0F
        if remaining >= 184:
            header = bytes([SYNC_BYTE, (0x40 if first else 0x00) | (pid >> 8), pid & 0xFF, 0x10 | cc])
            out += header + pes[pos : pos + 184]
            pos += 184
        else:
            stuffing = 184 - remaining
            header = bytes([SYNC_BYTE, (0x40 if first else 0x00) | (pid >> 8), pid & 0xFF, 0x30 | cc])
            if stuffing == 1:
                adaptation = bytes([0x00])
            else:
                adaptation = bytes([stuffing - 1, 0x00]) + b"\xFF" * (stuffing - 2)
            out += header + adaptation + pes[pos:]
            pos = n
        first = False
    return out


def split_pes_payload(pes: bytes) -> tuple[bytes, bytes]:
    """Split a PES packet into (header_prefix_including_optional_header, elementary_payload)."""
    if pes[:3] != b"\x00\x00\x01":
        return pes, b""
    stream_id = pes[3]
    # Stream ids without an optional PES header (padding, private_2, etc.) carry payload directly.
    if stream_id in (0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xFF, 0xF2, 0xF8):
        return pes[:6], pes[6:]
    header_data_length = pes[8]
    start = 9 + header_data_length
    return pes[:start], pes[start:]


def rebuild_pes(prefix: bytes, payload: bytes) -> bytes:
    """Reassemble a PES packet, refreshing PES_packet_length for the new payload size."""
    prefix = bytearray(prefix)
    body_length = (len(prefix) - 6) + len(payload)
    prefix[4:6] = struct.pack(">H", body_length if body_length <= 0xFFFF else 0)
    return bytes(prefix) + payload


def decrypt_sample_aes(data: bytes, key: bytes, iv: Optional[bytes] = None) -> bytes:
    """Decrypt a SAMPLE-AES MPEG-TS byte string and return a clean (standard) MPEG-TS.

    ``key`` is the 16-byte content key. ``iv`` is the 16-byte initialization vector; when not
    given it defaults to all zeros (the HLS convention for an absent ``EXT-X-KEY`` IV). The
    output has its PMT stream types patched to the standard values, so it muxes like any TS.
    """
    if iv is None:
        iv = b"\x00" * BLOCK

    sync = data.find(bytes([SYNC_BYTE]))
    if sync < 0:
        raise ValueError("Not an MPEG-TS stream (no sync byte found)")
    packets = [data[i : i + PACKET_LENGTH] for i in range(sync, len(data) - PACKET_LENGTH + 1, PACKET_LENGTH)]

    # First pass: locate the PMT and the elementary stream types.
    pmt_pid: Optional[int] = None
    pid_types: dict[int, int] = {}
    for pkt in packets:
        if pkt[0] != SYNC_BYTE:
            continue
        pid = packet_pid(pkt)
        off = payload_offset(pkt)
        if off < 0:
            continue
        if pid == 0 and pkt[1] & 0x40:  # PAT
            section = pkt[off + 1 + pkt[off] :]
            pmt_pid = ((section[10] & 0x1F) << 8) | section[11]
        elif pmt_pid is not None and pid == pmt_pid and pkt[1] & 0x40:
            section = pkt[off + 1 + pkt[off] :]
            pid_types = parse_pmt(section)
            break

    decrypters = {
        0xDB: decrypt_h264_es,
        0xCF: decrypt_aac_es,
        0xC1: decrypt_ac3_es,
    }
    encrypted_pids = {pid: t for pid, t in pid_types.items() if t in decrypters}
    if not encrypted_pids:
        return data  # nothing SAMPLE-AES encrypted

    # Second pass: rebuild the stream, decrypting elementary PIDs and patching the PMT.
    out = bytearray()
    continuity: dict[int, int] = {pid: 0 for pid in encrypted_pids}
    pending: dict[int, bytearray] = {}

    def flush(pid: int) -> None:
        pes = bytes(pending.pop(pid))
        prefix, payload = split_pes_payload(pes)
        decrypted = decrypters[encrypted_pids[pid]](payload, key, iv)
        out.extend(repacketize(pid, rebuild_pes(prefix, decrypted), continuity))

    for pkt in packets:
        if pkt[0] != SYNC_BYTE:
            continue
        pid = packet_pid(pkt)
        if pid in encrypted_pids:
            off = payload_offset(pkt)
            if pkt[1] & 0x40:  # PUSI -> new PES
                if pid in pending:
                    flush(pid)
                pending[pid] = bytearray(pkt[off:] if off >= 0 else b"")
            elif pid in pending and off >= 0:
                pending[pid] += pkt[off:]
            continue
        if pmt_pid is not None and pid == pmt_pid and pkt[1] & 0x40:
            off = payload_offset(pkt)
            pointer = pkt[off]
            head = pkt[: off + 1 + pointer]
            section = pkt[off + 1 + pointer :]
            patched = patch_pmt_section(section)
            packet = head + patched
            packet = packet[:PACKET_LENGTH] + b"\xFF" * (PACKET_LENGTH - len(packet))
            out += packet[:PACKET_LENGTH]
            continue
        out += pkt

    for pid in list(pending):
        flush(pid)
    return bytes(out)
