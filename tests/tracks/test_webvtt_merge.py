"""Regression tests for segmented-WebVTT merging (``core/utils/webvtt.py``).

The merge used to round-trip cues through pycaption's reader/writer (and a pysubs2
pre-normalization step), both of which silently dropped inline formatting despite
``preserve_formatting=True``. It is now a pure string-level splice: cue
timing lines, settings, and payload are kept verbatim, so span tags
(``<i>``/``<u>``/``<b>``), ASS-style overrides (``{\\an8}``), and entities all
survive, while duplicate headers collapse and boundary-duplicate cues dedupe.
"""

from __future__ import annotations

from unshackle.core.utils.webvtt import merge_segmented_webvtt

# Two segments, each a full WebVTT document with its own X-TIMESTAMP-MAP header,
# as they arrive concatenated from a DASH/HLS subtitle stream. The first cue carries
# span tags, an {\an8} override, a cue setting, and a literal &lt; entity.
SEGMENTED_VTT = (
    "WEBVTT\n"
    "X-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:00:00.000\n\n"
    "00:00:01.000 --> 00:00:03.000 line:10% align:center\n"
    "<i>Hello</i> {\\an8}world <u>und</u> <b>bold</b> 5 &lt; 6\n\n"
    "WEBVTT\n"
    "X-TIMESTAMP-MAP=MPEGTS:1800000,LOCAL:00:00:00.000\n\n"
    "00:00:04.000 --> 00:00:06.000\n"
    "<i>Second</i> cue\n"
)


def test_merge_preserves_inline_formatting():
    out = merge_segmented_webvtt(SEGMENTED_VTT, segment_durations=[900000, 900000], timescale=1)
    for tag in ("<i>", "</i>", "<u>", "</u>", "<b>", "</b>", "{\\an8}"):
        assert tag in out, f"{tag!r} was stripped from merged output:\n{out}"


def test_merge_preserves_cue_settings_and_entities():
    out = merge_segmented_webvtt(SEGMENTED_VTT, segment_durations=[900000, 900000], timescale=1)
    assert "line:10% align:center" in out  # cue positioning survives
    assert "&lt;" in out  # a literal entity is not turned into a stray tag


def test_merge_collapses_headers_into_single_document():
    out = merge_segmented_webvtt(SEGMENTED_VTT, segment_durations=[900000, 900000], timescale=1)
    assert out.count("WEBVTT") == 1
    assert "X-TIMESTAMP-MAP" not in out


def test_merge_dedupes_boundary_duplicate_cues():
    # A cue that spans a segment boundary is emitted in both segments; the merge
    # should collapse the adjacent identical pair into one.
    dup = SEGMENTED_VTT + (
        "\nWEBVTT\n"
        "X-TIMESTAMP-MAP=MPEGTS:2700000,LOCAL:00:00:00.000\n\n"
        "00:00:04.000 --> 00:00:06.000\n"
        "<i>Second</i> cue\n"
    )
    out = merge_segmented_webvtt(dup, segment_durations=[900000, 900000, 900000], timescale=1)
    assert out.count("<i>Second</i> cue") == 1, f"boundary duplicate not deduped:\n{out}"


def test_merge_keeps_timing_lines_verbatim():
    # String-level merge must not re-render timestamps (pycaption shortened
    # 00:00:01.000 -> 00:01.000) and must keep the settings tail untouched.
    out = merge_segmented_webvtt(SEGMENTED_VTT, segment_durations=[900000, 900000], timescale=1)
    assert "00:00:01.000 --> 00:00:03.000 line:10% align:center" in out
    assert "00:00:04.000 --> 00:00:06.000" in out


def test_merge_splices_triple_duplicate_into_one():
    # A cue spanning two boundaries appears in three consecutive segments; the
    # kept cue must be extended each time (latent bug: extending the dropped one).
    vtt = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nSame cue\n\n"
        "WEBVTT\n\n00:00:02.000 --> 00:00:03.000\nSame cue\n\n"
        "WEBVTT\n\n00:00:03.000 --> 00:00:04.000\nSame cue\n"
    )
    out = merge_segmented_webvtt(vtt)
    assert out.count("Same cue") == 1
    assert "00:00:01.000 --> 00:00:04.000" in out


def test_merge_drops_cue_ids_and_notes_keeps_style():
    vtt = (
        "WEBVTT\n\n"
        "STYLE\n::cue { color: red }\n\n"
        "NOTE a comment\n\n"
        "some-cue-id-42\n00:00:01.000 --> 00:00:02.000\nHello\n"
    )
    out = merge_segmented_webvtt(vtt)
    assert "Hello" in out
    assert "some-cue-id-42" not in out
    assert "NOTE" not in out
    assert "STYLE\n::cue { color: red }" in out  # kept; players without STYLE support ignore it


def test_merge_dedupes_repeated_style_blocks_before_cues():
    # Every segment repeats the same STYLE block; keep exactly one, before the first cue.
    style = "WEBVTT\n\nSTYLE\n::cue { color: red }\n\n"
    vtt = style + "00:00:01.000 --> 00:00:02.000\nOne\n\n" + style + "00:00:03.000 --> 00:00:04.000\nTwo\n"
    out = merge_segmented_webvtt(vtt)
    assert out.count("STYLE") == 1
    assert out.index("STYLE") < out.index("-->")


def test_merge_skips_malformed_timing_line():
    vtt = (
        "WEBVTT\n\n"
        "garbage --> not-a-time\nBroken\n\n"
        "00:00:01.000 --> 00:00:02.000\nGood\n"
    )
    out = merge_segmented_webvtt(vtt)
    assert "Good" in out
    assert "Broken" not in out
