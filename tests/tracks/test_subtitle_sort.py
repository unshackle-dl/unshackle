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
