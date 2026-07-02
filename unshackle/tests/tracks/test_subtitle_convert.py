"""Tests for the data-driven subtitle conversion registry (``tracks/subtitle_convert.py``).

Covers three things the refactor must guarantee:
- the capability matrix resolves the right backend chain per (source, target) and env
  (SubtitleEdit present or not),
- ``conversion_method`` pins a backend but still falls back (pin-then-fallback),
- styled SubStation (ASS/SSA) is never auto-downconverted to SRT unless explicitly forced.

Backends pysubs2/subby/pycaption are hard deps so the conversion paths run in CI without
SubtitleEdit; SubtitleEdit availability is simulated by patching ``binaries.SubtitleEdit``.
"""

from __future__ import annotations

import pathlib
import re
import struct

import pytest

from unshackle.core import binaries
from unshackle.core.tracks import subtitle_convert as sc
from unshackle.core.tracks.subtitle import Subtitle

Codec = Subtitle.Codec

VTT_SAMPLE = """WEBVTT

1
00:00:01.000 --> 00:00:02.000
Hello

2
00:00:03.000 --> 00:00:04.000
World
"""

ASS_SAMPLE = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,18,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\i1}Hello{\\i0}
Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,World
"""


@pytest.fixture(autouse=True)
def _no_subtitleedit(monkeypatch):
    """Default every test to a SubtitleEdit-less environment; tests opt in when needed."""
    monkeypatch.setattr(binaries, "SubtitleEdit", None)


def make_sub(tmp_path, name: str, text: str, codec: Codec) -> Subtitle:
    path = tmp_path / name
    path.write_text(text, encoding="utf8")
    sub = Subtitle(url="https://example.test/x", language="en", codec=codec)
    sub.path = path
    return sub


def cue_count(path) -> int:
    return len(re.findall(r"-->", path.read_text("utf8")))


# --- capability matrix / resolver -------------------------------------------------------


def test_resolve_webvtt_to_srt_order():
    chain = [b.name for b in sc.resolve_backends(Codec.WebVTT, Codec.SubRip)]
    assert chain == ["subby", "pysubs2", "pycaption"]


def test_resolve_ass_to_srt_only_pysubs2_without_subtitleedit():
    # subby and pycaption cannot read ASS, so only pysubs2 remains.
    chain = [b.name for b in sc.resolve_backends(Codec.SubStationAlphav4, Codec.SubRip)]
    assert chain == ["pysubs2"]


def test_subtitleedit_ranks_first_when_available(monkeypatch):
    monkeypatch.setattr(binaries, "SubtitleEdit", "/usr/bin/seconv")
    chain = [b.name for b in sc.resolve_backends(Codec.WebVTT, Codec.SubRip)]
    assert chain[0] == "subtitleedit"


def test_pin_then_fallback_orders_pin_first():
    chain = [b.name for b in sc.resolve_backends(Codec.WebVTT, Codec.SubRip, pin="pysubs2")]
    assert chain[0] == "pysubs2"
    assert "subby" in chain  # fallbacks remain after the pin


def test_pin_unavailable_falls_back_to_ranked_chain():
    # subtitleedit pinned but not installed -> just the ranked available backends.
    chain = [b.name for b in sc.resolve_backends(Codec.WebVTT, Codec.SubRip, pin="subtitleedit")]
    assert chain == ["subby", "pysubs2", "pycaption"]


def test_fallback_runs_when_first_backend_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("unshackle.core.config.config.subtitle", {}, raising=False)

    def boom(self, source, src, target, out):
        raise RuntimeError("backend exploded")

    # WebVTT->SRT chain is [subby, pysubs2, pycaption]; kill subby, expect pysubs2 to finish.
    monkeypatch.setattr(sc.SubbyBackend, "convert", boom)
    sub = make_sub(tmp_path, "x.vtt", VTT_SAMPLE, Codec.WebVTT)
    out = sub.convert(Codec.SubRip, forced=True)
    assert sub.codec == Codec.SubRip
    assert cue_count(out) == 2


def test_no_backend_for_unsupported_target_raises(tmp_path):
    sub = make_sub(tmp_path, "x.ass", ASS_SAMPLE, Codec.SubStationAlphav4)
    with pytest.raises(NotImplementedError):
        sub.convert(Codec.fVTT, forced=True)  # no backend writes segmented fVTT


# --- styled-ASS protection --------------------------------------------------------------


def test_ass_to_srt_kept_as_is_when_not_forced(tmp_path, monkeypatch):
    monkeypatch.setattr("unshackle.core.config.config.subtitle", {}, raising=False)
    sub = make_sub(tmp_path, "x.ass", ASS_SAMPLE, Codec.SubStationAlphav4)
    out = sub.convert(Codec.SubRip, forced=False)
    assert sub.codec == Codec.SubStationAlphav4  # unchanged
    assert out == sub.path
    assert out.suffix == ".ass"


def test_ass_to_srt_converts_when_forced(tmp_path, monkeypatch):
    monkeypatch.setattr("unshackle.core.config.config.subtitle", {}, raising=False)
    sub = make_sub(tmp_path, "x.ass", ASS_SAMPLE, Codec.SubStationAlphav4)
    out = sub.convert(Codec.SubRip, forced=True)
    assert sub.codec == Codec.SubRip
    assert out.suffix == ".srt"
    assert cue_count(out) == 2
    assert "{\\" not in out.read_text("utf8")  # override tags stripped


# --- conversion paths -------------------------------------------------------------------


def test_webvtt_to_srt_conversion(tmp_path, monkeypatch):
    monkeypatch.setattr("unshackle.core.config.config.subtitle", {}, raising=False)
    sub = make_sub(tmp_path, "x.vtt", VTT_SAMPLE, Codec.WebVTT)
    out = sub.convert(Codec.SubRip, forced=True)
    assert sub.codec == Codec.SubRip
    assert cue_count(out) == 2


def test_same_codec_is_noop(tmp_path):
    sub = make_sub(tmp_path, "x.srt", "1\n00:00:01,000 --> 00:00:02,000\nHi\n", Codec.SubRip)
    assert sub.convert(Codec.SubRip) == sub.path
    assert sub.codec == Codec.SubRip


# --- ASS/SSA font detection ------------------------------------------------------------

FONT_ASS = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Italic, Alignment, MarginV, Encoding
Style: Default,Trebuchet MS,24,&H00FFFFFF,0,0,2,18,1
Style: sign,@Arial Unicode MS,20,&H00FFFFFF,0,0,8,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\fnTimes New Roman}A sign
Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,{\\fntimes new roman}lower case
Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,{\\fnGeorgia\\b1}bold note
"""


def test_extract_fonts_styles_and_inline_overrides():
    fonts = Subtitle.extract_fonts(FONT_ASS)
    # Style fontnames (column located via Format line, @-prefix stripped) + inline \fn overrides
    assert fonts == {"Trebuchet MS", "Arial Unicode MS", "Times New Roman", "Georgia"}
    # case-insensitive de-dup keeps the mixed-case spelling, not "times new roman"
    assert "times new roman" not in fonts


def test_extract_fonts_handles_non_default_column_order():
    ass = (
        "[V4+ Styles]\n"
        "Format: Name, Fontsize, Fontname, Bold\n"  # Fontname not in the usual position
        "Style: Main,28,Verdana,0\n"
    )
    assert Subtitle.extract_fonts(ass) == {"Verdana"}


# --- non-Latin scripts (RTL / CJK) preserved through conversion ------------------------

CJK_RTL_VTT = """WEBVTT

1
00:00:01.000 --> 00:00:02.000
مرحبا بالعالم

2
00:00:03.000 --> 00:00:04.000
안녕하세요

3
00:00:05.000 --> 00:00:06.000
你好世界
"""


@pytest.mark.parametrize(
    "pattern",
    [r"[؀-ۿ]", r"[가-힣]", r"[一-鿿]"],  # Arabic, Hangul, CJK
)
def test_non_latin_scripts_survive_vtt_to_srt(tmp_path, monkeypatch, pattern):
    monkeypatch.setattr("unshackle.core.config.config.subtitle", {}, raising=False)
    sub = make_sub(tmp_path, "x.vtt", CJK_RTL_VTT, Codec.WebVTT)
    out = sub.convert(Codec.SubRip, forced=True)
    text = out.read_text("utf8")
    assert cue_count(out) == 3
    assert re.search(pattern, text)  # script survived the round-trip, no mojibake


# --- SDH stripping ----------------------------------------------------------------------

SDH_SRT = """1
00:00:01,000 --> 00:00:02,000
[door creaks]

2
00:00:03,000 --> 00:00:04,000
Hello there.

3
00:00:05,000 --> 00:00:06,000
♪ upbeat music ♪
"""


def test_sdh_stripping_removes_effects_keeps_dialogue(tmp_path, monkeypatch):
    # subby's SDHStripper runs on SRT without SubtitleEdit installed.
    monkeypatch.setattr("unshackle.core.config.config.subtitle", {"sdh_method": "subby"}, raising=False)
    sub = make_sub(tmp_path, "x.srt", SDH_SRT, Codec.SubRip)
    sub.strip_hearing_impaired()
    out = sub.path.read_text("utf8")
    assert "Hello there." in out  # real dialogue kept
    assert "door creaks" not in out  # bracketed effect removed (subby SDHStripper)


# --- segmented (box-encapsulated) formats: fVTT (wvtt) / fTTML (stpp) --------------------
# These ship from DASH/HLS as fragmented MP4 (e.g. HBO Max). The downloader concatenates
# init + media segments into one file; parse() reads the MP4 boxes directly.

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def caption_total(caption_set) -> int:
    return sum(len(caption_set.get_captions(lang)) for lang in caption_set.get_languages())


def build_stpp_mp4(*ttml_fragments: str) -> bytes:
    """A minimal stpp-style MP4: ftyp + one mdat per TTML fragment (what fTTML.parse reads)."""

    def box(box_type: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + box_type + payload

    data = box(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isomiso6")
    for frag in ttml_fragments:
        data += box(b"mdat", frag.encode("utf8"))
    return data


def test_segmented_fvtt_parses_and_converts(tmp_path, monkeypatch):
    monkeypatch.setattr("unshackle.core.config.config.subtitle", {}, raising=False)
    data = (FIXTURES / "segmented.wvtt.mp4").read_bytes()

    caption_set = Subtitle.parse(data, Codec.fVTT)
    assert caption_total(caption_set) == 2

    seg = tmp_path / "seg.wvtt"
    seg.write_bytes(data)
    sub = Subtitle(url="https://example.test/x", language="en", codec=Codec.fVTT)
    sub.path = seg
    # download() converts fVTT -> WebVTT (not "forced"); chain is subby then pycaption.
    out = sub.convert(Codec.WebVTT)
    assert sub.codec == Codec.WebVTT
    assert cue_count(out) == 2


def test_segmented_fttml_parses_and_converts(tmp_path, monkeypatch):
    monkeypatch.setattr("unshackle.core.config.config.subtitle", {}, raising=False)
    frag = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<tt xmlns="http://www.w3.org/ns/ttml" xml:lang="en"><body><div>'
        '<p begin="00:00:0{a}.000" end="00:00:0{b}.000">Line {a}</p>'
        "</div></body></tt>"
    )
    data = build_stpp_mp4(frag.format(a=1, b=2), frag.format(a=3, b=4))

    caption_set = Subtitle.parse(data, Codec.fTTML)
    assert caption_total(caption_set) == 2

    seg = tmp_path / "seg.stpp"
    seg.write_bytes(data)
    sub = Subtitle(url="https://example.test/x", language="en", codec=Codec.fTTML)
    sub.path = seg
    # download() converts fTTML -> TTML (only pycaption can read fTTML); then -> SRT.
    sub.convert(Codec.TimedTextMarkupLang)
    assert sub.codec == Codec.TimedTextMarkupLang
    out = sub.convert(Codec.SubRip, forced=True)
    assert cue_count(out) == 2
