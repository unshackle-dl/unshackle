"""``Audio.to_music_container`` must turn a fragmented MP4 into a playable, taggable file.

Music titles never reach the muxer, so the downloaded fragmented MP4 is what the user gets.
Its ``mvhd`` states a length of zero, so a player stops after the first fragment. FLAC
inside an MP4 is also not a FLAC stream. Each test builds a real MP4 with FFmpeg and reads
the result back with mutagen, which reads the header only, exactly as a player does.
FFprobe cannot prove this, because it scans every fragment and reports the true length
either way.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import mutagen
import pytest

from tests.core.test_music_tagger_roundtrip import make_song
from unshackle.core import binaries
from unshackle.core.music.tagger import write_music_metadata
from unshackle.core.tracks import Audio

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="requires ffmpeg")


def encode(path: Path, encoder: str, *extra_args: str) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=f=440:r=44100:d=6",
            "-ac",
            "2",
            "-c:a",
            encoder,
            "-strict",
            "-2",  # flac and opus in mp4 are flagged experimental by the muxer
            *extra_args,
            str(path),
        ],
        check=True,
    )
    return path


def fragmented_mp4(path: Path, encoder: str) -> Path:
    return encode(path, encoder, "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "1000000")


def header_length(path: Path) -> float:
    """The length the file states in its header, which is what a player and the tagger read."""
    return mutagen.File(path).info.length


@pytest.mark.parametrize(
    ("codec", "encoder", "extension"),
    [
        (Audio.Codec.AAC, "aac", ".m4a"),
        (Audio.Codec.ALAC, "alac", ".m4a"),
        (Audio.Codec.FLAC, "flac", ".flac"),
        (Audio.Codec.OPUS, "libopus", ".opus"),
        (Audio.Codec.MP3, "libmp3lame", ".mp3"),
    ],
)
def test_remux_states_the_full_length_in_the_codec_container(
    tmp_path: Path, codec: Audio.Codec, encoder: str, extension: str
) -> None:
    source = fragmented_mp4(tmp_path / "track.mp4", encoder)
    assert header_length(source) == 0.0, "the fixture must reproduce the fault the remux corrects"

    track = Audio(id_="a1", url="https://example.test/a1.mp4", language="en", codec=codec)
    track.path = source

    track.to_music_container()
    assert track.path.suffix == extension
    assert not source.exists()
    assert header_length(track.path) == pytest.approx(6.0, abs=0.5)
    assert write_music_metadata(track.path, make_song()).written, "the remuxed file must accept tags"


def test_flac_is_re_encoded_because_a_copy_states_no_length(tmp_path: Path) -> None:
    """A copy carries the source STREAMINFO over, whose sample count a fragmenting writer zeroes."""
    source = fragmented_mp4(tmp_path / "track.mp4", "flac")
    copied = tmp_path / "copied.flac"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-c",
            "copy",
            str(copied),
        ],
        check=True,
    )
    assert header_length(copied) == 0.0

    track = Audio(id_="a1", url="https://example.test/a1.mp4", language="en", codec=Audio.Codec.FLAC)
    track.path = source
    track.to_music_container()

    assert header_length(track.path) == pytest.approx(6.0, abs=0.5)
    assert mutagen.File(track.path).info.bits_per_sample == 16, "the re-encode must not change the sample format"


def test_dolby_digital_plus_takes_the_mp4_muxer(tmp_path: Path) -> None:
    """FFmpeg's ipod muxer refuses Dolby Digital Plus, so it goes straight to the plain MP4 muxer."""
    source = encode(tmp_path / "track.mp4", "eac3", "-ar", "48000")

    track = Audio(id_="a1", url="https://example.test/a1.mp4", language="en", codec=Audio.Codec.EC3)
    track.path = source

    track.to_music_container()
    assert track.path.suffix == ".m4a"
    assert header_length(track.path) == pytest.approx(6.0, abs=0.5)
    assert write_music_metadata(track.path, make_song()).written


def test_a_failed_remux_keeps_the_source_and_removes_its_partial_file(tmp_path: Path) -> None:
    """FFmpeg creates the output before it fails, and nothing else sweeps the temp directory."""
    # FFmpeg 9.0 writes PCM to MP4 as ipcm, so PCM no longer fails the remux.
    source = encode(tmp_path / "track.mp4", "adpcm_ms", "-f", "mov")  # no MP4 muxer takes ADPCM

    track = Audio(id_="a1", url="https://example.test/a1.mp4", language="en", codec=None)
    track.path = source

    with pytest.raises(RuntimeError):
        track.to_music_container()

    assert track.path == source and source.exists(), "a failed remux must keep the downloaded file"
    assert not list(tmp_path.glob("track_music*")), "a failed remux must not leave a partial file"


def make_track(path: Path, codec: Audio.Codec | None) -> Audio:
    track = Audio(id_="a1", url="https://example.test/a1.mp4", language="en", codec=codec)
    track.path = path
    return track


@pytest.mark.parametrize(
    ("encoder", "fmt", "declared", "extension", "probed"),
    [
        ("libopus", "webm", None, ".opus", Audio.Codec.OPUS),
        ("libvorbis", "webm", Audio.Codec.AAC, ".ogg", Audio.Codec.OGG),
        ("flac", "flac", Audio.Codec.AAC, ".flac", Audio.Codec.FLAC),
        ("libmp3lame", "mp3", None, ".mp3", Audio.Codec.MP3),
        ("pcm_s16le", "wav", None, ".flac", Audio.Codec.PCM),
        ("pcm_s24le", "wav", None, ".flac", Audio.Codec.PCM),
        ("pcm_f32le", "wav", None, ".wav", Audio.Codec.PCM),
    ],
)
def test_the_file_codec_decides_the_container(
    tmp_path: Path, encoder: str, fmt: str, declared: Audio.Codec | None, extension: str, probed: Audio.Codec
) -> None:
    """Every track downloads under a .mp4 name, and a service can declare no codec or the wrong one."""
    track = make_track(encode(tmp_path / "track.mp4", encoder, "-f", fmt), declared)

    track.to_music_container()
    assert track.path.suffix == extension
    assert track.codec == probed
    assert header_length(track.path) == pytest.approx(6.0, abs=0.5)
    assert write_music_metadata(track.path, make_song()).written


def test_24_bit_pcm_stays_24_bit_in_flac(tmp_path: Path) -> None:
    track = make_track(encode(tmp_path / "track.mp4", "pcm_s24le", "-f", "wav"), Audio.Codec.PCM)
    track.to_music_container()
    assert mutagen.File(track.path).info.bits_per_sample == 24


def test_dts_goes_to_matroska(tmp_path: Path) -> None:
    """FFmpeg writes DTS into MP4 with the wrong sample entry, so DTS takes Matroska."""
    track = make_track(encode(tmp_path / "track.mp4", "dca", "-ar", "48000", "-f", "dts"), None)

    track.to_music_container()
    assert track.path.suffix == ".mka"
    assert track.codec == Audio.Codec.DTS


def test_without_ffprobe_the_declared_codec_decides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "FFProbe", None)
    track = make_track(fragmented_mp4(tmp_path / "track.mp4", "aac"), Audio.Codec.AAC)

    track.to_music_container()
    assert track.path.suffix == ".m4a"


@pytest.mark.parametrize(("encoder", "extra"), [("flac", ()), ("eac3", ("-ar", "48000"))])
def test_a_sample_window_is_cut_in_the_remux(tmp_path: Path, encoder: str, extra: tuple[str, ...]) -> None:
    """The cut runs in the same step, so a FLAC header states the cut length and EC-3 takes the MP4 muxer."""
    source = encode(tmp_path / "track.mp4", encoder, *extra)
    track = make_track(source, None)

    track.to_music_container(start=1.0, duration=2.0)
    assert header_length(track.path) == pytest.approx(2.0, abs=0.3)


def test_the_remux_writes_no_encoder_tag(tmp_path: Path) -> None:
    track = make_track(encode(tmp_path / "track.mp4", "libmp3lame", "-f", "mp3"), Audio.Codec.MP3)
    track.to_music_container()
    tags = mutagen.File(track.path).tags
    assert not tags or "TSSE" not in tags


def test_float_pcm_goes_to_wav_whatever_its_bit_depth(tmp_path: Path) -> None:
    """16- and 24-bit float PCM report a bit depth FLAC can hold, but FLAC has no float samples."""
    track = make_track(encode(tmp_path / "track.mp4", "pcm_f32le", "-f", "wav"), Audio.Codec.PCM)
    stream = {"codec_name": "pcm_f24le", "sample_fmt": "flt", "bits_per_raw_sample": "24"}

    output = track.ffmpeg_music_container(track.path, stream, 0.0, None)

    assert output.suffix == ".wav"
    assert mutagen.File(output).info.bits_per_sample == 32


def test_a_sample_window_after_the_end_keeps_the_download(tmp_path: Path) -> None:
    source = encode(tmp_path / "track.mp4", "flac")
    track = make_track(source, None)

    with pytest.raises(ValueError):
        track.to_music_container(start=60.0, duration=30.0)
    assert track.path == source and source.exists()
    assert not list(tmp_path.glob("track_music*"))


@pytest.mark.skipif(binaries.MP4Box is None, reason="requires MP4Box")
def test_mp4box_writes_a_cut_m4a_and_leaves_no_temp_file(tmp_path: Path) -> None:
    track = make_track(fragmented_mp4(tmp_path / "track.mp4", "aac"), Audio.Codec.AAC)

    output = track.mp4box_music_container(track.path, 1.0, 2.0)

    assert output.suffix == ".m4a"
    assert header_length(output) == pytest.approx(2.0, abs=0.3)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["track.mp4", "track_music.m4a"]


def test_mp4box_route_removes_its_temp_file_when_preparation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def strip(source: Path, output: Path) -> bool:
        output.write_bytes(b"init")
        return True

    def fail(path: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(binaries, "MP4Box", Path("MP4Box"))
    monkeypatch.setattr("unshackle.core.tracks.track.strip_duplicate_init_boxes", strip)
    monkeypatch.setattr("unshackle.core.tracks.track.fix_data_reference_indexes", fail)
    track = make_track(fragmented_mp4(tmp_path / "track.mp4", "aac"), Audio.Codec.AC4)

    with pytest.raises(OSError):
        track.mp4box_music_container(track.path, 0.0, None)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["track.mp4"]
