from __future__ import annotations

import logging
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.core.test_hdr10plus_probe import ts
from unshackle.commands.dl import take_unmuxable_audio, track_file_suffix
from unshackle.core.tracks import Audio, Tracks, Video
from unshackle.core.utils import audio_vivid

LOG = logging.getLogger("test")


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


def test_the_elementary_stream_is_every_pes_payload_in_order(tmp_path: Path) -> None:
    frames = [bytes([n]) * size for n, size in ((1, 40), (2, 400), (3, 175))]
    source, out = tmp_path / "a.ts", tmp_path / "a.av3a"
    source.write_bytes(audio_vivid_ts(frames))
    assert audio_vivid.extract_audio_vivid(source, out)
    assert out.read_bytes() == b"".join(frames)


def test_a_ts_without_audio_vivid_extracts_nothing(tmp_path: Path) -> None:
    source = tmp_path / "a.ts"
    source.write_bytes(ts((0x0F, b"")))
    assert not audio_vivid.extract_audio_vivid(source, tmp_path / "a.av3a")


def fake_decoder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> None:
    """A stand-in decoder that copies its input to its output, or exits 3; a .cmd on Windows."""
    if sys.platform == "win32":
        decoder = tmp_path / "avs3Decoder.cmd"
        decoder.write_text("@exit /b 3\r\n" if fail else '@copy /y "%~1" "%~2" >nul\r\n')
    else:
        decoder = tmp_path / "avs3Decoder"
        decoder.write_text("#!/bin/sh\nexit 3\n" if fail else '#!/bin/sh\ncp "$1" "$2"\n')
        decoder.chmod(decoder.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(audio_vivid.binaries, "avs3Decoder", decoder, raising=False)


def test_the_decoder_gets_the_stream_and_writes_the_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_decoder(tmp_path, monkeypatch)
    source = tmp_path / "a.ts"
    source.write_bytes(audio_vivid_ts([b"frame"]))
    wav = audio_vivid.decode_audio_vivid(source, LOG)
    assert wav == tmp_path / "a.wav" and wav.read_bytes() == b"frame"
    assert not (tmp_path / "a.av3a").exists()


def test_a_failing_decoder_warns_once_and_leaves_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake_decoder(tmp_path, monkeypatch, fail=True)
    source = tmp_path / "a.ts"
    source.write_bytes(audio_vivid_ts([b"frame"]))
    with caplog.at_level(logging.WARNING):
        assert audio_vivid.decode_audio_vivid(source, LOG) is None
    assert caplog.messages == ["Audio Vivid decode of a.ts failed"]
    assert sorted(p.name for p in tmp_path.iterdir() if not p.name.startswith("avs3")) == ["a.ts"]


def tracks_with(tmp_path: Path, *codecs: Any) -> Tracks:
    tracks = Tracks()
    tracks.videos = [Video(url="https://x.example/v.m3u8", language="zh", codec=Video.Codec.HEVC)]
    for n, codec in enumerate(codecs):
        audio = Audio(url=f"https://x.example/a{n}.m3u8", language="zh", codec=codec, id_=f"a{n}")
        audio.path = tmp_path / f"a{n}.ts"
        tracks.audio.append(audio)
    return tracks


def test_without_a_decoder_audio_vivid_leaves_the_mux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unshackle.commands.dl.decode_audio_vivid", lambda path, log: None)
    tracks = tracks_with(tmp_path, Audio.Codec.EC3, Audio.Codec.AV3A)
    kept = take_unmuxable_audio(tracks, LOG)
    assert [a.id for a in kept] == ["a1"]
    assert [a.codec for a in tracks.audio] == [Audio.Codec.EC3]


def test_with_a_decoder_a_wav_copy_joins_the_mux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unshackle.commands.dl.decode_audio_vivid", lambda path, log: path.with_suffix(".wav"))
    tracks = tracks_with(tmp_path, Audio.Codec.AV3A)
    kept = take_unmuxable_audio(tracks, LOG)
    assert [a.id for a in kept] == ["a0"] and kept[0].codec == Audio.Codec.AV3A
    assert [(a.id, a.codec, a.path) for a in tracks.audio] == [("a0", Audio.Codec.PCM, tmp_path / "a0.wav")]


def test_a_kept_track_is_named_like_a_no_mux_track(tmp_path: Path) -> None:
    tracks = tracks_with(tmp_path, Audio.Codec.AV3A)
    assert [track_file_suffix(t) for t in (tracks.videos[0], tracks.audio[0])] == [".HEVC", ".zh.AV3A"]
