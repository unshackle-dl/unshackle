from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from unshackle.core import binaries
from unshackle.core.utils.hdr10plus import PROBE_BYTES, TS_PACKET, ts_payload, ts_pid, ts_pmt
from unshackle.core.utils.subprocess import run_step


def extract_audio_vivid(ts_path: Path, out_path: Path) -> bool:
    """Write the Audio Vivid elementary stream of an MPEG-TS file to ``out_path``; False when it holds none."""
    from unshackle.core.tracks import Audio

    with ts_path.open("rb") as ts:
        entries = ts_pmt(ts.read(PROBE_BYTES)) or []
        pid = next((p for p, kind, reg in entries if Audio.Codec.from_ts(kind, reg) == Audio.Codec.AV3A), None)
        if pid is None:
            return False
        ts.seek(0)
        with out_path.open("wb") as out:
            while packet := ts.read(TS_PACKET):
                if len(packet) < TS_PACKET or packet[0] != 0x47 or ts_pid(packet) != pid:
                    continue
                payload = ts_payload(packet)
                if packet[1] & 0x40 and payload[:3] == b"\x00\x00\x01" and len(payload) > 8:
                    payload = payload[9 + payload[8] :]
                out.write(payload)
    return out_path.stat().st_size > 0


def decode_audio_vivid(ts_path: Path, log: logging.Logger) -> Optional[Path]:
    """Decode an Audio Vivid MPEG-TS file to WAV with the user's ``avs3Decoder``, or None without one or on failure.

    unshackle does not ship the decoder: the UWA reference decoder's licence limits its use and redistribution.
    A service that delivers Audio Vivid declares it in ``get_binaries()``, so ``env check`` lists it.
    """
    decoder: Optional[Path] = getattr(binaries, "avs3Decoder", None)
    if not decoder:
        return None
    stream, wav = ts_path.with_suffix(".av3a"), ts_path.with_suffix(".wav")
    try:
        if not extract_audio_vivid(ts_path, stream):
            raise ValueError("no Audio Vivid stream in the file")
        run_step([decoder, stream, wav], status="Decoding Audio Vivid...", output=wav, label="Audio Vivid decode")
    except (OSError, ValueError, RuntimeError) as e:
        log.warning(f"Audio Vivid decode of {ts_path.name} failed")
        log.debug(f"Audio Vivid decode of {ts_path.name}: {e}")
        return None
    finally:
        stream.unlink(missing_ok=True)
    return wav
