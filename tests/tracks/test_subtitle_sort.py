import pytest

from unshackle.core.tracks import Subtitle, Tracks


def sub(lang: str, forced: bool = False, sdh: bool = False, cc: bool = False, orig: bool = False) -> Subtitle:
    track = Subtitle(
        url=f"https://example.test/{lang}-{forced}-{sdh}-{cc}",
        language=lang,
        codec=Subtitle.Codec.SubRip,
        forced=forced,
        sdh=sdh,
        cc=cc,
    )
    track.is_original_lang = orig
    return track


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


def langs(tracks: Tracks) -> list[str]:
    return [str(s.language) for s in tracks.subtitles]


@pytest.fixture
def many() -> Tracks:
    t = Tracks()
    t.add([sub(lang, orig=lang == "ja") for lang in ("ar", "bg", "en", "es", "fr", "ja")])
    return t


def test_priority_orders_listed_then_rest_alphabetically(many: Tracks) -> None:
    many.sort_subtitles(by_language=["en", "es", "fr"], group_by="language")
    assert langs(many) == ["en", "es", "fr", "ar", "bg", "ja"]


def test_priority_outranks_the_original_language(many: Tracks) -> None:
    # "all" resolves to the original language (ja), which must stay below the named ones
    many.sort_subtitles(by_language=["en", "es", "all"], group_by="language")
    assert langs(many) == ["en", "es", "ja", "ar", "bg", "fr"]


def test_priority_matches_regional_variants() -> None:
    t = Tracks()
    t.add([sub("ar"), sub("en-US"), sub("es-419"), sub("ja")])
    t.sort_subtitles(by_language=["en", "es"], group_by="language")
    assert langs(t) == ["en-US", "es-419", "ar", "ja"]


def test_priority_overlapping_entries_are_harmless() -> None:
    t = Tracks()
    t.add([sub("ar"), sub("en-GB"), sub("en-US"), sub("es"), sub("ja")])
    t.sort_subtitles(by_language=["en", "en-US", "es"], group_by="language")
    assert langs(t) == ["en-GB", "en-US", "es", "ar", "ja"]


def test_no_priority_leaves_order_unchanged(many: Tracks) -> None:
    many.sort_subtitles(by_language=[], group_by="language")
    assert langs(many) == ["ar", "bg", "en", "es", "fr", "ja"]


def test_all_alone_still_floats_the_original_language(many: Tracks) -> None:
    many.sort_subtitles(by_language=["all"], group_by="language")
    assert langs(many) == ["ja", "ar", "bg", "en", "es", "fr"]


@pytest.fixture
def regionals() -> Tracks:
    t = Tracks()
    t.add([sub("ar"), sub("en"), sub("en-GB"), sub("en-US"), sub("ja")])
    return t


def test_fuzzy_priority_floats_regional_variants(regionals: Tracks) -> None:
    regionals.sort_subtitles(by_language=["en"], group_by="language")
    assert langs(regionals) == ["en", "en-GB", "en-US", "ar", "ja"]


def test_exact_priority_floats_only_the_named_tag(regionals: Tracks) -> None:
    # CLDR rates en/en-US at distance 0, so exact mode needs the RFC 4647 string preference
    # to hold en-US back; without it en-US floats and en-GB does not.
    regionals.sort_subtitles(by_language=["en"], group_by="language", exact_match=True)
    assert langs(regionals) == ["en", "ar", "en-GB", "en-US", "ja"]


def test_exact_priority_still_falls_back_when_no_tag_is_string_equal() -> None:
    t = Tracks()
    t.add([sub("ar"), sub("cmn"), sub("ja")])
    t.sort_subtitles(by_language=["zh"], group_by="language", exact_match=True)
    assert langs(t) == ["cmn", "ar", "ja"]


@pytest.mark.parametrize("exact", [False, True])
def test_unparseable_priority_entry_is_ignored(many: Tracks, exact: bool) -> None:
    # a config typo in language_priority must not crash the sort
    many.sort_subtitles(by_language=["en", "engrish", "f", ""], group_by="language", exact_match=exact)
    assert langs(many) == ["en", "ar", "bg", "es", "fr", "ja"]
