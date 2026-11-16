from __future__ import annotations

from pathlib import Path
from typing import Any

from pymediainfo import MediaInfo

from tests.core.test_hdr10plus_probe import ts
from unshackle.commands.dl import no_mux_extension, take_unmuxable_audio, track_file_suffix
from unshackle.core.tracks import Audio, Tracks, Video


def media_packet(pid: int, payload: bytes, start: bool) -> bytes:
    """One TS packet carrying ``payload``, padded with adaptation-field stuffing as packagers do."""
    stuffing = 184 - len(payload)
    flags = 0x40 if start else 0x00
    if stuffing:
        field = bytes([stuffing - 1]) + (b"\x00" + b"\xff" * (stuffing - 2) if stuffing > 1 else b"")
        return bytes([0x47, flags | pid >> 8, pid & 0xFF, 0x30]) + field + payload
    return bytes([0x47, flags | pid >> 8, pid & 0xFF, 0x10]) + payload


def audio_vivid_ts(frames: list[bytes]) -> bytes:
    """A PAT, a PMT with one Audio Vivid stream on PID 0x100, and one PES per frame."""
    data = ts((0xD5, b""))
    for frame in frames:
        pes = b"\x00\x00\x01\xc0\x00\x00\x80\x80\x05" + b"\x21\x00\x01\x00\x01" + frame
        data += b"".join(media_packet(0x100, pes[i : i + 184], i == 0) for i in range(0, len(pes), 184))
    return data


def tracks_with(tmp_path: Path, *codecs: Any) -> Tracks:
    tracks = Tracks()
    tracks.videos = [Video(url="https://x.example/v.m3u8", language="zh", codec=Video.Codec.HEVC)]
    for n, codec in enumerate(codecs):
        audio = Audio(url=f"https://x.example/a{n}.m3u8", language="zh", codec=codec, id_=f"a{n}")
        audio.path = tmp_path / f"a{n}.ts"
        tracks.audio.append(audio)
    return tracks


def test_audio_vivid_leaves_the_mux(tmp_path: Path) -> None:
    tracks = tracks_with(tmp_path, Audio.Codec.EC3, Audio.Codec.AV3A)
    kept = take_unmuxable_audio(tracks)
    assert [a.id for a in kept] == ["a1"]
    assert [a.codec for a in tracks.audio] == [Audio.Codec.EC3]


def test_a_kept_track_is_named_like_a_no_mux_track(tmp_path: Path) -> None:
    tracks = tracks_with(tmp_path, Audio.Codec.AV3A)
    assert [track_file_suffix(t) for t in (tracks.videos[0], tracks.audio[0])] == [".HEVC", ".zh.AV3A"]


def test_a_kept_ts_file_saved_as_mp4_gets_the_ts_extension(tmp_path: Path) -> None:
    source = tmp_path / "Audio_a0.mp4"
    source.write_bytes(audio_vivid_ts([b"frame"] * 40))
    track = tracks_with(tmp_path, Audio.Codec.AV3A).audio[0]
    assert no_mux_extension(track, source, MediaInfo.parse(source)) == ".ts"
