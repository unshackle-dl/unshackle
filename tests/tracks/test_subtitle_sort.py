import pytest

from unshackle.core.tracks import Subtitle, Tracks


def sub(lang: str, forced: bool = False, sdh: bool = False, cc: bool = False) -> Subtitle:
    return Subtitle(
        url=f"https://example.test/{lang}-{forced}-{sdh}-{cc}",
        language=lang,
        codec=Subtitle.Codec.SubRip,
        forced=forced,
        sdh=sdh,
        cc=cc,
    )


def shape(tracks: Tracks) -> list[tuple[str, str]]:
    return [
        (str(s.language), "forced" if s.forced else "sdh" if (s.sdh or s.cc) else "normal") for s in tracks.subtitles
    ]


@pytest.fixture
def tracks() -> Tracks:
    t = Tracks()
    t.add([sub("en", sdh=True), sub("en", forced=True), sub("en"), sub("it", forced=True), sub("it")])
    return t


def test_default_order_is_forced_normal_sdh(tracks: Tracks) -> None:
    tracks.sort_subtitles(by_language=["en"])
    assert shape(tracks) == [
        ("en", "forced"),
        ("en", "normal"),
        ("en", "sdh"),
        ("it", "forced"),
        ("it", "normal"),
    ]


def test_type_priority_overrides_group_order(tracks: Tracks) -> None:
    tracks.sort_subtitles(by_language=["en"], type_priority=["normal", "sdh", "forced"])
    assert shape(tracks) == [
        ("en", "normal"),
        ("en", "sdh"),
        ("en", "forced"),
        ("it", "normal"),
        ("it", "forced"),
    ]


def test_type_priority_unlisted_types_fall_to_end(tracks: Tracks) -> None:
    tracks.sort_subtitles(by_language=["en"], type_priority=["normal"])
    # unlisted types keep their pre-sort relative order (stable sort)
    assert shape(tracks) == [
        ("en", "normal"),
        ("en", "sdh"),
        ("en", "forced"),
        ("it", "normal"),
        ("it", "forced"),
    ]


def test_cc_counts_as_sdh() -> None:
    t = Tracks()
    t.add([sub("en", cc=True), sub("en")])
    t.sort_subtitles(by_language=["en"], type_priority=["normal", "sdh"])
    assert shape(t) == [("en", "normal"), ("en", "sdh")]


@pytest.fixture
def unprioritised() -> Tracks:
    """Languages that no by_language argument floats to the top."""
    t = Tracks()
    t.add([sub("fi", sdh=True), sub("fi"), sub("de", sdh=True), sub("de")])
    return t


def test_type_grouping_separates_a_language_from_its_sdh(unprioritised: Tracks) -> None:
    unprioritised.sort_subtitles()
    assert shape(unprioritised) == [
        ("de", "normal"),
        ("fi", "normal"),
        ("de", "sdh"),
        ("fi", "sdh"),
    ]


def test_language_grouping_keeps_a_language_with_its_sdh(unprioritised: Tracks) -> None:
    unprioritised.sort_subtitles(group_by="language")
    assert shape(unprioritised) == [
        ("de", "normal"),
        ("de", "sdh"),
        ("fi", "normal"),
        ("fi", "sdh"),
    ]


def test_language_grouping_still_honours_type_priority_and_sections(tracks: Tracks) -> None:
    # de sits outside by_language, where type-major sorting would interleave it with en
    tracks.add([sub("de"), sub("de", sdh=True)])
    tracks.sort_subtitles(by_language=["it"], type_priority=["normal", "sdh", "forced"], group_by="language")
    assert shape(tracks) == [
        ("it", "normal"),
        ("it", "forced"),
        ("de", "normal"),
        ("de", "sdh"),
        ("en", "normal"),
        ("en", "sdh"),
        ("en", "forced"),
    ]


def test_language_grouping_keeps_regional_variants_adjacent_through_sections() -> None:
    # "en" floats en-GB and en-US as one block, so each exact tag keeps its variants
    t = Tracks()
    t.add(
        [
            sub("en-US", sdh=True),
            sub("fi"),
            sub("en-GB"),
            sub("en-US"),
            sub("fi", sdh=True),
            sub("en-GB", forced=True),
            sub("en-GB", sdh=True),
        ]
    )
    t.sort_subtitles(by_language=["en"], group_by="language")
    assert shape(t) == [
        ("en-GB", "forced"),
        ("en-GB", "normal"),
        ("en-GB", "sdh"),
        ("en-US", "normal"),
        ("en-US", "sdh"),
        ("fi", "normal"),
        ("fi", "sdh"),
    ]
