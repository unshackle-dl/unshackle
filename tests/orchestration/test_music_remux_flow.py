"""A Song must end as a playable, correctly named, correctly tagged music file.

``remux_music_tracks`` (``unshackle/commands/dl.py``) is the hook the download loop runs
on every title after repack and before naming. These tests drive the real hook, then the
real naming, move, and tagging seams in the order ``dl.result()`` composes them. Only the
surrounding Click command does not run, because it needs a service, a CDM, and a network.
Every music-specific decision it makes lives in the seams driven here.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import mutagen
import pytest
from mutagen.flac import FLAC
from pymediainfo import MediaInfo

from tests.core.test_music_container import fragmented_mp4, header_length
from tests.core.test_music_tagger_roundtrip import make_song
from unshackle.commands.dl import remux_music_tracks
from unshackle.core.config import config
from unshackle.core.tracks import Audio, Tracks
from unshackle.core.utils import tags

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="requires ffmpeg")

log = logging.getLogger("test_music_remux_flow")


@pytest.fixture
def default_templates():
    """Pin the naming templates, because the suite shares the live global config."""
    saved = (config.output_template, config.folder_templates, config.folder_template, config.tag_imdb_tmdb)
    config.output_template = {"songs": "{track_number}. {title}"}
    config.folder_templates = {}
    config.folder_template = ""
    config.tag_imdb_tmdb = False  # keep provider lookups out of the test
    yield
    config.output_template, config.folder_templates, config.folder_template, config.tag_imdb_tmdb = saved


def make_audio(path: Path, codec: Audio.Codec, track_id: str) -> Audio:
    track = Audio(id_=track_id, url=f"https://example.test/{track_id}.mp4", language="en", codec=codec)
    track.path = path
    return track


def test_the_hook_remuxes_every_audio_track_of_a_song(tmp_path: Path) -> None:
    song = make_song()
    song.tracks = Tracks(
        [
            make_audio(fragmented_mp4(tmp_path / "Audio_a1.mp4", "flac"), Audio.Codec.FLAC, "a1"),
            make_audio(fragmented_mp4(tmp_path / "Audio_a2.mp4", "aac"), Audio.Codec.AAC, "a2"),
        ]
    )

    remux_music_tracks(song, log)

    assert [t.path.suffix for t in song.tracks.audio] == [".flac", ".m4a"]
    for track in song.tracks.audio:
        assert header_length(track.path) == pytest.approx(6.0, abs=0.5)


def test_one_broken_track_warns_and_does_not_stop_the_rest(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    broken = tmp_path / "Audio_a1.mp4"
    broken.write_bytes(b"")  # ffmpeg cannot read this, so its remux must fail
    song = make_song()
    song.tracks = Tracks(
        [
            make_audio(broken, Audio.Codec.AAC, "a1"),
            make_audio(fragmented_mp4(tmp_path / "Audio_a2.mp4", "aac"), Audio.Codec.AAC, "a2"),
        ]
    )

    with caplog.at_level(logging.WARNING, logger=log.name):
        remux_music_tracks(song, log)

    assert "Could not remux audio track a1" in caplog.text
    assert song.tracks.audio[0].path == broken, "a failed remux must keep the downloaded file"
    assert song.tracks.audio[1].path.suffix == ".m4a", "the failure must not stop the other tracks"


def test_a_non_song_title_is_left_alone(tmp_path: Path) -> None:
    source = fragmented_mp4(tmp_path / "Audio_a1.mp4", "aac")
    track = make_audio(source, Audio.Codec.AAC, "a1")

    class NotASong:
        tracks = Tracks([track])

    remux_music_tracks(NotASong(), log)

    assert track.path == source


def test_a_fragmented_flac_song_ends_playable_named_and_tagged(tmp_path: Path, default_templates: None) -> None:
    """The full post-download flow: hook, MediaInfo naming, final move, music tagging."""
    song = make_song()
    source = fragmented_mp4(tmp_path / "Audio_track-0001.mp4", "flac")  # the downloader's naming scheme
    track = make_audio(source, Audio.Codec.FLAC, "track-0001")
    song.tracks = Tracks([track])
    assert header_length(source) == 0.0, "the fixture must reproduce the fault the remux corrects"

    remux_music_tracks(song, log)

    # the naming and move seams, with the arguments dl.result() hands them
    media_info = MediaInfo.parse(track.path)
    final_dir = tmp_path / "downloads" / song.get_filename(media_info, show_service=False, folder=True)
    final_dir.mkdir(parents=True)
    final_path = final_dir / f"{song.get_filename(media_info, show_service=False)}{track.path.suffix}"
    shutil.move(track.path, final_path)

    tags.tag_file(final_path, song)

    assert final_path == final_dir / "04.Example.Track.flac"
    assert not source.exists(), "the fragmented source must be gone"
    assert mutagen.File(final_path).info.length == pytest.approx(6.0, abs=0.5)
    audio = FLAC(final_path)
    assert audio["TITLE"] == ["Example Track"]
    assert audio["ARTIST"] == ["Example Artist"]
