import json
from types import SimpleNamespace

import pytest

from unshackle.core import binaries
from unshackle.core.tracks import Tracks, Video
from unshackle.core.tracks.tracks import file_track_ids


def make_video(track_id: str, **data) -> Video:
    video = Video(
        id_=track_id,
        url=f"https://example.test/{track_id}.m3u8",
        language="zh",
        codec=Video.Codec.AVC,
        range_=Video.Range.SDR,
        width=1920,
        height=1080,
        bitrate=5_000_000,
    )
    video.data.update(data)
    return video


def downloaded(video: Video, tmp_path) -> Video:
    video.path = tmp_path / "v.ts"
    video.path.write_bytes(b"\x00" * 16)
    return video


def mux_until_mkvmerge_check(tracks: Tracks) -> None:
    # the embedded-audio metadata pass runs before the mkvmerge lookup, so a missing
    # binary stops the mux right after the state under test has been written
    with pytest.raises(RuntimeError, match="MKVToolNix"):
        tracks.mux("Test Title")


def test_names_undeclared_video(monkeypatch):
    monkeypatch.setattr(binaries, "MKVToolNix", None)
    video = make_video("v1")
    tracks = Tracks(video)

    mux_until_mkvmerge_check(tracks)

    assert video.needs_repack
    assert video.data["audio_language"] == "zh"
    assert video.embedded_audio_name() == "Chinese"


def test_service_declaration_is_preserved(monkeypatch):
    monkeypatch.setattr(binaries, "MKVToolNix", None)
    declared = make_video("v1", audio_language="th", audio_language_name="Thai [Original]")
    undeclared = make_video("v2")
    tracks = Tracks([declared, undeclared])

    mux_until_mkvmerge_check(tracks)

    assert declared.data["audio_language"] == "th"
    assert declared.data["audio_language_name"] == "Thai [Original]"
    assert not declared.needs_repack
    assert undeclared.data["audio_language"] == "zh"


def test_embedded_audio_without_a_name_gets_the_language_name(tmp_path, mux_args):
    args = mux_args(Tracks(downloaded(make_video("v1", audio_language="pt-BR"), tmp_path)), {"video": 0, "audio": 1})
    assert args[args.index("--track-name") + 1] == "1:Portuguese"


def test_embedded_audio_options_use_the_file_track_ids(tmp_path, mux_args):
    video = make_video("v1", audio_language="ko", audio_language_name="Korean")
    args = mux_args(Tracks(downloaded(video, tmp_path)), {"audio": 0, "video": 1})
    assert args[args.index("--track-name") + 1] == "0:Korean"
    original = f"{video.is_original_lang}"
    assert [args[i + 1] for i, arg in enumerate(args) if arg == "--original-flag"] == [f"1:{original}", f"0:{original}"]
    assert "0:ko" in args and "1:zh" in args


def test_embedded_audio_options_are_left_out_when_the_file_has_no_audio(tmp_path, mux_args):
    args = mux_args(Tracks(downloaded(make_video("v1", audio_language="ko"), tmp_path)), {"video": 0})
    assert "--track-name" not in args


def test_repack_and_mux_share_the_embedded_audio_name():
    assert make_video("v1", audio_language="zh").embedded_audio_name() == "Chinese"
    assert make_video("v1", audio_language="zh", audio_language_name="Mandarin").embedded_audio_name() == "Mandarin"


def test_file_track_ids_reads_the_first_id_of_each_type(monkeypatch, tmp_path):
    tracks = [{"id": 0, "type": "audio"}, {"id": 1, "type": "video"}, {"id": 2, "type": "audio"}]
    reply = SimpleNamespace(stdout=json.dumps({"tracks": tracks}))
    monkeypatch.setattr("unshackle.core.tracks.tracks.subprocess.run", lambda *a, **k: reply)
    assert file_track_ids(tmp_path / "v.ts") == {"audio": 0, "video": 1}
    monkeypatch.setattr("unshackle.core.tracks.tracks.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=""))
    assert file_track_ids(tmp_path / "v.ts") == {"video": 0, "audio": 1}
