import textwrap

import pytest

from unshackle.core.constants import SPACED_AUDIO_CODECS
from unshackle.core.manifests.hls import HLS
from unshackle.core.tracks.audio import Audio

# The codecs one variant declares, in manifest order. One GROUP-ID holds renditions of
# all three, which is why a group-wide codec cannot be right.
DTS_X_GROUP = [
    ("dtsx", Audio.Codec.DTSX),
    ("ec-3", Audio.Codec.EC3),
    ("mp4a", Audio.Codec.AAC),
]


def test_dtsc_maps_to_dts() -> None:
    assert Audio.Codec.from_mime("dtsc") == Audio.Codec.DTS


@pytest.mark.parametrize("mime", ["dtsx", "dtsy"])
def test_dts_uhd_sample_entries_map_to_dtsx(mime: str) -> None:
    """DTS-UHD is a separate bitstream, so it must not share the DTS name."""
    assert Audio.Codec.from_mime(mime) == Audio.Codec.DTSX
    assert Audio.Codec.from_codecs(f"avc1.64001f,{mime},ec-3,mp4a.40.2") == Audio.Codec.DTSX
    assert Audio.Codec.DTSX.value == "DTS-X", "':' is not a legal path character on Windows"


def test_unknown_mime_still_raises() -> None:
    with pytest.raises(ValueError):
        Audio.Codec.from_mime("dtsz")


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        # Rendition URIs that name the codec in one "_" delimited field.
        ("r/composite_448k_dtsx_en_PRIMARY_de37f606-8b62-4116-bdec-0de711f2b7d0_x.m3u8", Audio.Codec.DTSX),
        ("r/composite_256k_ec-3_cs_PRIMARY_b4556be9-7eb3-47a9-b103-d97263a99ec1_x.m3u8", Audio.Codec.EC3),
        ("r/composite_128k_mp4a.40.2_en_NARRATION_3891a283-94c0-4b89-9dc8-a70fcec25661_x.m3u8", Audio.Codec.AAC),
        # A UUID can spell "ec-3", so a substring match would call this rendition EC3.
        ("r/composite_128k_mp4a.40.2_en_PRIMARY_b4556be9-7eb3-47a9-b103-d97263a9ec-3_x.m3u8", Audio.Codec.AAC),
        # Packagers that name the codec as a directory, hyphen or not.
        ("https://cdn/x/dtsx-448k/index.m3u8", Audio.Codec.DTSX),
        ("https://cdn/x/dtsx/index.m3u8", Audio.Codec.DTSX),
        # Nothing names a codec, so the first codec the variant declares wins.
        ("r/audio-en.m3u8", Audio.Codec.DTSX),
    ],
)
def test_select_audio_codec_matches_delimited_codecs(uri: str, expected: Audio.Codec) -> None:
    assert HLS.select_audio_codec(DTS_X_GROUP, uri) == expected


def test_select_audio_codec_will_not_read_one_codec_out_of_another() -> None:
    """ "ac-3" sits inside the "eac-3" that names an E-AC-3 rendition."""
    candidates = [("ec-3", Audio.Codec.EC3), ("ac-3", Audio.Codec.AC3)]
    assert HLS.select_audio_codec(candidates, "https://cdn/x/audio_ac-3_192k.m3u8") == Audio.Codec.AC3
    # No codec matches, so this falls back to the first declared rather than to AC3.
    assert HLS.select_audio_codec(candidates, "https://cdn/x/audio_eac-3_768k.m3u8") == Audio.Codec.EC3


def test_select_audio_codec_ignores_the_query_string() -> None:
    assert HLS.select_audio_codec(DTS_X_GROUP, "r/composite_448k_dtsx_en.m3u8?fallback=mp4a") == Audio.Codec.DTSX


def test_select_audio_codec_holds_a_single_declaration() -> None:
    """One declared codec needs no URI, so a group of one is never guessed at."""
    assert HLS.select_audio_codec([("mp4a", Audio.Codec.AAC)], "r/anything_dtsx.m3u8") == Audio.Codec.AAC
    assert HLS.select_audio_codec([], "r/x.m3u8") is None
    assert HLS.select_audio_codec(None, None) is None


def test_spaced_codec_constants_name_real_codecs() -> None:
    """The constant holds bare strings, so a rename must not silently stop matching."""
    for value in SPACED_AUDIO_CODECS:
        assert value in {c.value for c in Audio.Codec}, f"{value} is not an Audio.Codec value"


def test_a_short_ladder_rung_cannot_hide_a_group_codec() -> None:
    """
    One GROUP-ID is reached from every variant that references it, and a low rung may
    declare fewer codecs than the group holds. The union has to survive the short rung.
    """
    manifest = textwrap.dedent("""\
        #EXTM3U
        #EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="A",NAME="English",LANGUAGE="en",CHANNELS="10",URI="composite_448k_dtsx_en.m3u8"
        #EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="A",NAME="Czech",LANGUAGE="cs",CHANNELS="6",URI="composite_256k_ec-3_cs.m3u8"
        #EXT-X-STREAM-INF:BANDWIDTH=9000000,CODECS="hvc1.2.4.L150.90,dtsx,ec-3",AUDIO="A"
        v_high.m3u8
        #EXT-X-STREAM-INF:BANDWIDTH=400000,CODECS="avc1.64001f,ec-3",AUDIO="A"
        v_low.m3u8
    """)
    tracks = HLS.from_text(manifest, "https://example.test/master.m3u8").to_tracks("en")
    by_lang = {str(t.language): t.codec for t in tracks.audio}
    assert by_lang["en"] == Audio.Codec.DTSX, "the low rung dropped dtsx from the group"
    assert by_lang["cs"] == Audio.Codec.EC3


def test_the_group_fallback_does_not_depend_on_variant_order() -> None:
    """
    A rendition URI that names no codec falls back to a codec the group declares. Which
    one must not depend on where the packager put its variants: ascending by BANDWIDTH is
    as common as descending, and picking the lossy label makes the premium rendition
    unreachable through --acodec.
    """
    variants = [
        '#EXT-X-STREAM-INF:BANDWIDTH=9000000,CODECS="hvc1.2.4.L150.90,dtsx,ec-3",AUDIO="A"\nv_high.m3u8',
        '#EXT-X-STREAM-INF:BANDWIDTH=400000,CODECS="avc1.64001f,ec-3",AUDIO="A"\nv_low.m3u8',
    ]
    header = (
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="A",NAME="English",LANGUAGE="en",CHANNELS="10",URI="audio_en.m3u8"\n'
    )
    for order in (variants, list(reversed(variants))):
        manifest = header + "\n".join(order) + "\n"
        tracks = HLS.from_text(manifest, "https://example.test/master.m3u8").to_tracks("en")
        assert [t.codec for t in tracks.audio] == [Audio.Codec.DTSX], f"variant order decided the codec: {order[0]!r}"
