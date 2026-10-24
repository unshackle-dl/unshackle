from pathlib import Path
from typing import Optional

import pytest
from langcodes import Language

from unshackle.core import binaries
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video
from unshackle.core.utilities import language_name, multi_script_languages, with_script


def sub(lang: str, name: Optional[str] = None, sdh: bool = False) -> Subtitle:
    return Subtitle(id_="s", url="https://x/s", codec=Subtitle.Codec.WebVTT, language=lang, name=name, sdh=sdh)


def test_multi_script_languages_come_from_cldr() -> None:
    assert {"zh", "yue", "sr"} <= multi_script_languages()
    assert not {"en", "pt", "ja"} & multi_script_languages()


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("zh", "zh"),
        ("zh-TW", "zh-Hant-TW"),
        ("zh-CN", "zh-Hans-CN"),
        ("zh-HK", "zh-Hant-HK"),
        ("zh-SG", "zh-Hans-SG"),
        ("zh-Hant", "zh-Hant"),
        ("yue-HK", "yue-Hant-HK"),
        ("sr-ME", "sr-Latn-ME"),
        ("sr-RS", "sr-Cyrl-RS"),
        ("pa-PK", "pa-Arab-PK"),
        ("ms-MY", "ms-MY"),
        ("en-US", "en-US"),
        ("und-TW", "und-TW"),
        ("und", "und"),
    ],
)
def test_with_script(tag: str, expected: str) -> None:
    assert str(with_script(Language.get(tag))) == expected


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("zh-Hant-TW", "Chinese (Traditional)"),
        ("zh-Hans-CN", "Chinese (Simplified)"),
        ("zh-Hant-HK", "Chinese (Traditional, Hong Kong)"),
        ("zh-TW", "Chinese (Taiwan)"),
        ("pt-BR", "Portuguese"),
        ("pt-PT", "Portuguese (Portugal)"),
        ("en-US", "English"),
        ("en-GB", "English (United Kingdom)"),
        ("ja-JP", "Japanese"),
        ("fr-CA", "French (Canada)"),
        ("ar-001", "Arabic"),
        ("es-419", "Spanish (Latin America)"),
        ("en", "English"),
        ("de-DE", "German"),
        ("en-ZZ", "English"),
        ("en-XA", "English"),
        ("en-Zzzz", "English"),
        ("sq-XK", "Albanian (Kosovo)"),
        ("mn-Mong-CN", "Mongolian (China)"),
        ("und", None),
        ("qaa", None),
        ("x-foo", None),
    ],
)
def test_language_name(tag: str, expected: Optional[str]) -> None:
    assert language_name(Language.get(tag)) == expected


def test_subtitle_names_and_tags_the_script_but_keeps_its_language() -> None:
    track = sub("zh-TW")
    assert track.name is None
    assert track.get_track_name() == "Chinese (Traditional)"
    assert str(track.mux_language) == "zh-Hant-TW"
    assert str(track.language) == "zh-TW"


def test_subtitle_flag_follows_the_default_name() -> None:
    assert sub("zh-CN", sdh=True).get_track_name() == "Chinese (Simplified) (SDH)"


def test_audio_gets_no_script() -> None:
    track = Audio(id_="a", url="https://x/a", codec=Audio.Codec.AAC, language="zh-TW", bitrate=1)
    assert track.get_track_name() == "Chinese (Taiwan)"
    assert str(track.mux_language) == "zh-TW"


def test_a_service_name_wins() -> None:
    assert sub("zh", name="简体中文").get_track_name() == "简体中文"


def test_mkvmerge_gets_the_subtitle_script_and_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Stop(Exception):
        pass

    captured: list[list[str]] = []

    def fake_popen(args: list[str], **kwargs: object) -> None:
        captured.append(args)
        raise Stop

    monkeypatch.setattr("unshackle.core.tracks.tracks.subprocess.Popen", fake_popen)
    monkeypatch.setattr(binaries, "MKVToolNix", Path("mkvmerge"))
    video = Video(id_="v", url="https://x/v", codec=Video.Codec.AVC, language="zh-TW", width=1920, height=1080)
    audio = Audio(id_="a", url="https://x/a", codec=Audio.Codec.AAC, language="zh-TW", bitrate=1)
    subtitle = sub("zh-TW")
    for track, file in ((video, "v.mp4"), (audio, "a.mp4"), (subtitle, "s.vtt")):
        track.path = tmp_path / file
        track.path.write_bytes(b"\x00" * 16)

    with pytest.raises(Stop):
        Tracks([video, audio, subtitle]).mux("T", delete=False, output_path=tmp_path / "o.mkv")

    def last_option(file: str, flag: str) -> str:
        """Return the value of the last flag given before the file, which is the one mkvmerge applies to it."""
        before = captured[0][: captured[0].index(str(tmp_path / file))]
        return before[len(before) - before[::-1].index(flag)]

    assert last_option("s.vtt", "--language") == "0:zh-Hant-TW"
    assert last_option("s.vtt", "--track-name") == "0:Chinese (Traditional)"
    assert last_option("a.mp4", "--language") == "0:zh-TW"


def test_the_default_name_follows_a_later_language_change() -> None:
    track = sub("yue")
    track.language = Language.get("zh-Hant")
    assert track.get_track_name() == "Chinese (Traditional)"


def test_a_language_assigned_as_text_later_is_parsed() -> None:
    track = sub("pt")
    track.language = "pt-PT"
    assert track.language == Language.get("pt-PT")
    assert track.get_track_name() == "Portuguese (Portugal)"
    assert str(track) == "SUB | [VTT] | pt-PT | Portuguese (Portugal)"
    track.language = None
    assert str(track.language) == "und"
    assert track.get_track_name() is None
