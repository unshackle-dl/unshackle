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
    ],
)
def test_remux_states_the_full_length_in_the_codec_container(
    tmp_path: Path, codec: Audio.Codec, encoder: str, extension: str
) -> None:
    source = fragmented_mp4(tmp_path / "track.mp4", encoder)
    assert header_length(source) == 0.0, "the fixture must reproduce the fault the remux corrects"

    track = Audio(id_="a1", url="https://example.test/a1.mp4", language="en", codec=codec)
    track.path = source

    assert track.to_music_container() is True
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


def test_dolby_digital_plus_falls_back_to_the_mp4_muxer(tmp_path: Path) -> None:
    """A .m4a name picks FFmpeg's ipod muxer, which refuses Dolby Digital Plus; the remux must not fail on it."""
    source = encode(tmp_path / "track.mp4", "eac3", "-ar", "48000")

    track = Audio(id_="a1", url="https://example.test/a1.mp4", language="en", codec=Audio.Codec.EC3)
    track.path = source

    assert track.to_music_container() is True
    assert track.path.suffix == ".m4a"
    assert header_length(track.path) == pytest.approx(6.0, abs=0.5)
    assert write_music_metadata(track.path, make_song()).written


def test_a_failed_remux_keeps_the_source_and_removes_its_partial_file(tmp_path: Path) -> None:
    """FFmpeg creates the output before it fails, and nothing else sweeps the temp directory."""
    # FFmpeg 9.0 writes PCM to MP4 as ipcm, so PCM no longer fails the remux.
    source = encode(tmp_path / "track.mp4", "adpcm_ms", "-f", "mov")  # no MP4 muxer takes ADPCM

    track = Audio(id_="a1", url="https://example.test/a1.mp4", language="en", codec=None)
    track.path = source

    with pytest.raises(subprocess.CalledProcessError):
        track.to_music_container()

    assert track.path == source and source.exists(), "a failed remux must keep the downloaded file"
    assert not list(tmp_path.glob("track_music*")), "a failed remux must not leave a partial file"
