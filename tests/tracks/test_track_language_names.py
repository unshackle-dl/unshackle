from pathlib import Path
from typing import Callable, Optional

import pytest
from langcodes import Language

from unshackle.core.config import config
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


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("zh-Hant-TW", "中文（繁體）"),
        ("zh-Hans-CN", "中文（简体）"),
        ("zh-Hant-HK", "中文（繁體，香港）"),
        ("zh-Hans-MO", "中文（简体，澳门）"),
        ("zh-TW", "中文（台灣）"),
        ("zh-HK", "中文（香港）"),
        ("cmn-Hant", "中文（繁體）"),
        ("yue-Hant", "粵語（繁體）"),
        ("sr-ME", "Srpski (Crna Gora)"),
        ("ka", "ქართული"),
        ("hy", "Հայերեն"),
        ("pt-BR", "Português"),
        ("pt-PT", "Português (Portugal)"),
        ("es-419", "Español (Latinoamérica)"),
        ("sr-Latn-ME", "Srpski (latinica, Crna Gora)"),
        ("ja-JP", "日本語"),
        ("en-GB", "English (United Kingdom)"),
        ("en-HK", "English (Hong Kong)"),
        ("qaa", None),
    ],
)
def test_native_language_name(tag: str, expected: Optional[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "muxing", {"track_names": "native"})
    assert language_name(Language.get(tag)) == expected


def test_native_subtitle_name_keeps_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "muxing", {"track_names": "native"})
    assert sub("zh-TW", sdh=True).get_track_name() == "中文（繁體） (SDH)"
    assert sub("zh", name="Mandarin").get_track_name() == "Mandarin"


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


def test_mkvmerge_gets_the_subtitle_script_and_name(tmp_path: Path, mux_args: Callable[..., list[str]]) -> None:
    video = Video(id_="v", url="https://x/v", codec=Video.Codec.AVC, language="zh-TW", width=1920, height=1080)
    audio = Audio(id_="a", url="https://x/a", codec=Audio.Codec.AAC, language="zh-TW", bitrate=1)
    subtitle = sub("zh-TW")
    for track, file in ((video, "v.mp4"), (audio, "a.mp4"), (subtitle, "s.vtt")):
        track.path = tmp_path / file
        track.path.write_bytes(b"\x00" * 16)

    args = mux_args(Tracks([video, audio, subtitle]), output_path=tmp_path / "o.mkv")

    def last_option(file: str, flag: str) -> str:
        """Return the value of the last flag given before the file, which is the one mkvmerge applies to it."""
        before = args[: args.index(str(tmp_path / file))]
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
