"""subtitle.ass_style and --sub-font: the look given to subtitles converted into ASS."""

from __future__ import annotations

import pysubs2
import pytest

from unshackle.commands.dl import dl, set_subtitle_look, subtitle_look
from unshackle.core import binaries
from unshackle.core.api.handlers import validate_download_parameters
from unshackle.core.tracks import subtitle_convert as sc
from unshackle.core.tracks.subtitle import Subtitle
from unshackle.core.tracks.video import Video

Codec = Subtitle.Codec

VTT = """WEBVTT

00:00:01.000 --> 00:00:02.000
[door creaks]

00:00:03.000 --> 00:00:04.000
Hello there.
"""

NATIVE_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,18,1
Style: Top,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,-1,0,0,100,100,0,0,1,2,1,8,10,10,18,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\pos(320,50)}Sign
Dialogue: 0,0:00:03.00,0:00:04.00,Top,,0,0,0,,Hello
"""


@pytest.fixture(autouse=True)
def no_subtitleedit(monkeypatch):
    monkeypatch.setattr(binaries, "SubtitleEdit", None)


def make_sub(tmp_path, name: str, text: str, codec: Codec, style: pysubs2.SSAStyle | None = None) -> Subtitle:
    path = tmp_path / name
    path.write_text(text, encoding="utf8")
    sub = Subtitle(url="https://example.test/x", language="en", codec=codec)
    sub.path = path
    sub.ass_style = style
    sub.ass_video_size = (1440, 1080)
    return sub


def test_style_fields_override_the_defaults():
    style = sc.build_ass_style({"fontname": "My Font", "marginv": 40, "primarycolor": "&H0000FFFF"})
    assert (style.fontname, style.fontsize, style.bold, style.marginv) == ("My Font", 72, True, 40)
    assert style.primarycolor == pysubs2.Color(255, 255, 0, 0)
    assert style.backcolor == pysubs2.Color(0, 0, 0, 0xA0)


@pytest.mark.parametrize(
    "fields",
    [
        {"fontsize_px": 50},
        {"primarycolor": "white"},
        {"bold": "false"},
        {"fontname": "A,B"},
        {"fontname": " "},
        {"marginv": "low"},
        {"fontsize": None},
        {"fontsize": {}},
        {"fontsize": -5},
        {"fontsize": float("nan")},
        {"outline": float("inf")},
        {"marginv": True},
        {"marginv": 3.6},
        {"primarycolor": 16777215},
        {"fontname": "A\u2028B"},
        {"fontname": "A\x00B"},
        {"alignment": 10},
    ],
)
def test_bad_style_fields_raise(fields):
    with pytest.raises(ValueError):
        sc.build_ass_style(fields)


def test_ass_field_names_are_accepted():
    style = sc.build_ass_style({"Fontname": "My Font", "PrimaryColour": "&H0000FFFF", "MarginV": 40.0})
    assert (style.fontname, style.primarycolor, style.marginv) == ("My Font", pysubs2.Color(255, 255, 0, 0), 40)


def test_the_look_goes_only_to_subtitles_that_are_not_already_ass(tmp_path):
    style = sc.build_ass_style({})
    vtt = make_sub(tmp_path, "x.vtt", VTT, Codec.WebVTT)
    ass = make_sub(tmp_path, "x.ass", NATIVE_ASS, Codec.SubStationAlphav4)
    videos = [
        Video(url="https://example.test/v", language="en"),
        Video(url="https://example.test/v", language="en", width=3840, height=1600),
    ]
    set_subtitle_look([vtt, ass], style, videos)
    assert (vtt.ass_style, ass.ass_style) == (style, None)
    assert vtt.ass_video_size == ass.ass_video_size == (3840, 1600)


def test_a_stripped_styled_track_with_no_cues_left_stays_srt(tmp_path, monkeypatch):
    monkeypatch.setattr("unshackle.core.config.config.subtitle", {"sdh_method": "auto"}, raising=False)
    sub = make_sub(tmp_path, "x.vtt", VTT.split("00:00:03")[0], Codec.WebVTT, sc.build_ass_style({}))
    sub.convert(Codec.SubStationAlphav4)
    sub.strip_hearing_impaired()
    assert sub.codec == Codec.SubRip


def test_a_stripped_native_ass_track_is_not_restyled(tmp_path, monkeypatch):
    monkeypatch.setattr("unshackle.core.config.config.subtitle", {"sdh_method": "auto"}, raising=False)
    sub = make_sub(tmp_path, "x.ass", NATIVE_ASS, Codec.SubStationAlphav4)
    set_subtitle_look([sub], sc.build_ass_style({"fontname": "My Font"}), [])
    sub.strip_hearing_impaired()
    assert sub.codec == Codec.SubRip


def test_vtt_to_ass_gets_the_style_on_a_canvas_shaped_like_the_video(tmp_path):
    sub = make_sub(tmp_path, "x.vtt", VTT, Codec.WebVTT, sc.build_ass_style({"fontname": "My Font"}))
    sub.convert(Codec.SubStationAlphav4)

    subs = pysubs2.load(str(sub.path))
    assert (subs.info["PlayResX"], subs.info["PlayResY"]) == ("1440", "1080")
    assert (subs.info["LayoutResX"], subs.info["LayoutResY"]) == ("1440", "1080")
    assert subs.styles["Default"].fontname == "My Font"
    assert subs.styles["Default"].fontsize == 72


def test_vtt_to_ass_without_a_style_is_left_as_converted(tmp_path):
    sub = make_sub(tmp_path, "x.vtt", VTT, Codec.WebVTT)
    sub.convert(Codec.SubStationAlphav4)

    subs = pysubs2.load(str(sub.path))
    assert "PlayResY" not in subs.info
    assert subs.styles["Default"].fontname == "Arial"


@pytest.mark.parametrize("target", [Codec.SubStationAlphav4, Codec.SubStationAlpha])
def test_a_native_ass_track_is_never_restyled(tmp_path, target):
    sub = make_sub(tmp_path, "x.ass", NATIVE_ASS, Codec.SubStationAlphav4, sc.build_ass_style({}))
    sub.convert(target)

    subs = pysubs2.load(str(sub.path))
    assert subs.info["PlayResY"] == "360"
    assert subs.styles["Default"].fontsize == 20
    assert "\\pos(320,50)" in subs.events[0].text


def test_a_converter_canvas_is_kept_and_the_style_scales_to_it(tmp_path):
    path = tmp_path / "x.ass"
    path.write_text(NATIVE_ASS, encoding="utf8")
    sc.style_substation(path, sc.build_ass_style({"fontname": "My Font"}), (1920, 1080))

    subs = pysubs2.load(str(path))
    assert (subs.info["PlayResX"], subs.info["PlayResY"]) == ("640", "360")
    assert subs.styles["Default"].fontsize == pytest.approx(24)
    assert subs.styles["Default"].marginv == 20
    top = subs.styles["Top"]
    assert (top.fontname, top.alignment, top.italic) == ("My Font", pysubs2.Alignment.TOP_CENTER, True)


def test_restyling_twice_gives_the_same_file(tmp_path):
    sub = make_sub(tmp_path, "x.vtt", VTT, Codec.WebVTT, sc.build_ass_style({}))
    sub.convert(Codec.SubStationAlphav4)
    first = sub.path.read_text("utf8")
    sc.style_substation(sub.path, sub.ass_style, sub.ass_video_size)
    assert sub.path.read_text("utf8") == first


def test_a_stripped_styled_track_stays_styled_ass(tmp_path, monkeypatch):
    monkeypatch.setattr("unshackle.core.config.config.subtitle", {"sdh_method": "auto"}, raising=False)
    sub = make_sub(tmp_path, "x.vtt", VTT, Codec.WebVTT, sc.build_ass_style({"fontname": "My Font"}))
    sub.convert(Codec.SubStationAlphav4)
    sub.strip_hearing_impaired()

    assert sub.codec == Codec.SubStationAlphav4
    subs = pysubs2.load(str(sub.path))
    assert subs.styles["Default"].fontname == "My Font"
    assert [e.plaintext for e in subs.events] == ["Hello there."]


@pytest.mark.parametrize(
    ("sub_format", "sub_font", "fields", "expected"),
    [
        (None, None, None, (None, None)),
        (None, None, {"fontsize": 60}, (Codec.SubStationAlphav4, "Arial")),
        (None, "My Font", None, (Codec.SubStationAlphav4, "My Font")),
        (Codec.SubStationAlpha, "My Font", {"fontname": "Other"}, (Codec.SubStationAlpha, "My Font")),
        (Codec.SubRip, None, {"fontsize": 60}, (Codec.SubRip, None)),
        ("original", None, {"fontsize": 60}, ("original", None)),
    ],
)
def test_subtitle_look_picks_the_format_and_style(sub_format, sub_font, fields, expected):
    out_format, style = subtitle_look(sub_format, sub_font, fields)
    assert (out_format, style.fontname if style else None) == expected


def test_a_typed_font_overrides_a_format_from_config():
    out_format, style = subtitle_look(Codec.SubRip, "My Font", None, typed_format=False)
    assert (out_format, style.fontname if style else None) == (Codec.SubStationAlphav4, "My Font")


def test_a_font_from_config_is_ignored_with_another_format():
    assert subtitle_look(Codec.SubRip, "My Font", None, typed_font=False) == (Codec.SubRip, None)
    assert subtitle_look("original", "My Font", {"fontsize": 60}, typed_font=False) == ("original", None)


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"sub_font": "My Font", "sub_format": "SRT"}, "Invalid sub_font: --sub-font needs ASS subtitles"),
        ({"sub_font": "A,B"}, "Invalid sub_font: ass_style fontname"),
    ],
)
def test_the_api_refuses_a_sub_font_it_cannot_use(data, error):
    assert (validate_download_parameters(data) or "").startswith(error)


def test_the_api_takes_a_sub_font_with_ass():
    assert validate_download_parameters({"sub_font": "My Font", "sub_format": "ASS"}) is None


@pytest.mark.parametrize(
    ("sub_format", "fields"),
    [(Codec.SubRip, None), ("original", None), (None, "Arial,72"), (None, {"bold": "yes"})],
)
def test_subtitle_look_refuses_a_style_it_cannot_use(sub_format, fields):
    with pytest.raises(ValueError):
        subtitle_look(sub_format, "My Font" if fields is None else None, fields)


@pytest.mark.parametrize("name", ["..", "../other"])
def test_a_font_name_that_is_a_path_finds_no_custom_font(tmp_path, monkeypatch, name):
    monkeypatch.setattr("unshackle.core.config.config.directories.fonts", tmp_path / "fonts", raising=False)
    for folder in ("fonts/My Font", "other"):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "x.ttf").write_bytes(b"")
    (tmp_path / "x.ttf").write_bytes(b"")
    assert dl.find_custom_font(None, "My Font") == tmp_path / "fonts/My Font/x.ttf"  # type: ignore[arg-type]
    assert dl.find_custom_font(None, name) is None  # type: ignore[arg-type]
