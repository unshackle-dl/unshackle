from types import SimpleNamespace

import pytest

from unshackle.core.utilities import embedded_audio_langs


def video(**data):
    return SimpleNamespace(data=data)


def test_no_videos():
    assert embedded_audio_langs([], True) == []


def test_video_without_a_declaration():
    assert embedded_audio_langs([video(), video(audio_language="")], True) == []


def test_declared_language_is_returned():
    assert embedded_audio_langs([video(audio_language="zh-CN")], True) == ["zh-CN"]


def test_every_declaring_video_counts():
    videos = [video(audio_language="zh-CN"), video(), video(audio_language="th")]
    assert embedded_audio_langs(videos, True) == ["zh-CN", "th"]


@pytest.mark.parametrize("videos", [[video(audio_language="zh-CN")], [video(audio_language="zh-CN"), video()]])
def test_dropping_the_video_drops_its_audio(videos):
    # --audio-only / --no-video discard the track the audio lives in, so nothing is embedded
    assert embedded_audio_langs(videos, False) == []
