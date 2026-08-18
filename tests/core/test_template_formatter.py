"""Separator cleanup around omitted conditionals in TemplateFormatter.

An omitted `{var?}` must not leave separator debris behind. Trailing spaces are the
dangerous case: the formatter sanitizes each path segment separately, and Windows
drops a trailing space when it creates a directory but not when it looks one up, so
the download fails with FileNotFoundError [WinError 3]. See #107 and #144.
"""

from __future__ import annotations

import pytest

from unshackle.core.config import config
from unshackle.core.titles.episode import Episode
from unshackle.core.titles.movie import Movie
from unshackle.core.titles.music import Song
from unshackle.core.utilities import sanitize_filename
from unshackle.core.utils.template_formatter import TemplateFormatter, detect_spacer


@pytest.mark.parametrize(
    ("template", "context", "expected"),
    [
        # An omitted conditional inside brackets must take the whole group with it.
        ("{title} ({year?})", {"title": "Some Movie", "year": ""}, "Some Movie"),
        ("{title} [{quality?}]", {"title": "Some Movie", "quality": ""}, "Some Movie"),
        ("{title} ({year?}) [{quality?}]", {"title": "T", "year": "", "quality": ""}, "T"),
        # The group survives when the value is present.
        ("{title} ({year?})", {"title": "Some Movie", "year": "2024"}, "Some Movie (2024)"),
        # #107: the dash wins, so the release tag stays attached.
        (
            "{title}.{year}.{atmos?}-{tag}",
            {"title": "Some Movie", "year": "2024", "atmos": "", "tag": "GRP"},
            "Some.Movie.2024-GRP",
        ),
        (
            "{title}.{year}.{atmos?}-{tag}",
            {"title": "Some Movie", "year": "2024", "atmos": "Atmos", "tag": "GRP"},
            "Some.Movie.2024.Atmos-GRP",
        ),
        # A conditional between two dashes must not leave a doubled separator.
        ("{title} - {edition?} - {year}", {"title": "A", "edition": "", "year": "2024"}, "A - 2024"),
        # Consecutive omitted conditionals collapse to nothing.
        ("{a?} {b?} {title}", {"a": "", "b": "", "title": "T"}, "T"),
        ("{show} S{season}E{ep} {name?}", {"show": "Show", "season": "01", "ep": "02", "name": ""}, "Show S01E02"),
    ],
)
def test_omitted_conditional_leaves_no_separator_debris(template, context, expected):
    assert TemplateFormatter(template).format(context) == expected


@pytest.mark.parametrize(
    ("template", "context"),
    [
        ("{title} ({year?})", {"title": "Some Movie", "year": ""}),
        ("{title} [{quality?}]", {"title": "Some Movie", "quality": ""}),
        ("{title}.{quality?}", {"title": "Some Movie", "quality": ""}),
        ("{title} - {tag?}", {"title": "Some Movie", "tag": ""}),
    ],
)
def test_result_is_safe_as_a_windows_path_segment(template, context):
    """Windows strips these when creating a directory but not when looking one up."""
    result = TemplateFormatter(template).format(context)
    assert result == result.strip(" ."), f"unsafe path segment: {result!r}"


def test_empty_result_falls_back():
    assert TemplateFormatter("{title?}").format({"title": ""}) == "untitled"


def test_missing_required_variable_raises():
    with pytest.raises(ValueError, match="Missing required template variables"):
        TemplateFormatter("{title}.{year}").format({"title": "T"})


@pytest.mark.parametrize(
    ("raw", "spacer", "expected"),
    [
        ("Some Movie ", " ", "Some Movie"),
        (" Some Movie", " ", "Some Movie"),
        ("Some Movie...", ".", "Some.Movie"),
        ("S.W.A.T.", ".", "S.W.A.T"),
        ("...Some Movie...", ".", "Some.Movie"),
    ],
)
def test_sanitize_filename_strips_unsafe_edges(raw, spacer, expected):
    assert sanitize_filename(raw, spacer) == expected


def test_sanitize_filename_keeps_interior_dots():
    assert sanitize_filename("Mr. Robot", ".") == "Mr.Robot"


@pytest.mark.parametrize(
    ("template", "context", "expected"),
    [
        # Space-separated style.
        ("{title} ({year})", {"title": "Some Movie", "year": "2024"}, "Some Movie (2024)"),
        (
            "{title} {year} {quality} {source}",
            {"title": "Some Movie", "year": "2024", "quality": "1080p", "source": "WEB-DL"},
            "Some Movie 2024 1080p WEB-DL",
        ),
        # Dot-separated style.
        (
            "{title}.{year}.{quality}.{source}",
            {"title": "Some Movie", "year": "2024", "quality": "1080p", "source": "WEB-DL"},
            "Some.Movie.2024.1080p.WEB-DL",
        ),
        # Interior dots in the title itself survive both styles.
        ("{title}.{year}", {"title": "Mr. Robot", "year": "2015"}, "Mr.Robot.2015"),
        ("{title} ({year})", {"title": "Mr. Robot", "year": "2015"}, "Mr. Robot (2015)"),
        ("{title}.{year}", {"title": "S.W.A.T.", "year": "2017"}, "S.W.A.T.2017"),
        ("{title} ({year})", {"title": "S.W.A.T.", "year": "2017"}, "S.W.A.T. (2017)"),
        # A decimal inside a codec name is not a separator.
        (
            "{title}.{year}.{audio}-{tag}",
            {"title": "Some Movie", "year": "2024", "audio": "DDP5.1", "tag": "GRP"},
            "Some.Movie.2024.DDP5.1-GRP",
        ),
    ],
)
def test_separator_styles_keep_interior_spacing(template, context, expected):
    assert TemplateFormatter(template).format(context) == expected


class DummyService:
    pass


class StubMediaInfo:
    """Minimal MediaInfo stand-in: the template context only reads track lists."""

    video_tracks: list = []
    audio_tracks: list = []


@pytest.fixture
def folder_config(monkeypatch):
    """Clear the global template config, and restore it after each test."""
    monkeypatch.setattr(config, "folder_templates", {})
    monkeypatch.setattr(config, "folder_template", "")
    monkeypatch.setattr(config, "output_template", {})
    return config


def make_movie():
    return Movie(id_="movie-0001", service=DummyService, name="Some Movie")


def make_episode(**overrides):
    kwargs = dict(id_="episode-0001", service=DummyService, title="Some Show", season=1, number=2, name="Pilot")
    kwargs.update(overrides)
    return Episode(**kwargs)


def make_song(**overrides):
    kwargs = dict(
        id_="track-0001",
        service=DummyService,
        name="Some Track",
        artist="Some Artist",
        album="Some Album",
        track=1,
        disc=1,
        year=2025,
    )
    kwargs.update(overrides)
    return Song(**kwargs)


def make_album_song(**overrides):
    kwargs = dict(
        id_="track-0001",
        service=DummyService,
        name="Some Track",
        artist="Some Artist",
        album="Some Album",
        track=1,
        disc=1,
        year=2025,
        album_artist="Some Artist",
    )
    kwargs.update(overrides)
    return Song(**kwargs)


def assert_safe_path(result: str):
    """No segment may start or end with a space or dot, or Windows loses the directory."""
    segments = result.split("/")
    assert all(segments), f"empty segment in {result!r}"
    assert all(seg == seg.strip(" .") for seg in segments), f"unsafe segment in {result!r}"


@pytest.mark.parametrize(
    ("kind", "make_title", "template", "expected"),
    [
        ("movies", make_movie, "Movies/{title} ({year?})", "Movies/Some Movie"),
        ("series", make_episode, "Shows/{title} ({year?})/Season {season}", "Shows/Some Show/Season S01"),
        ("songs", make_song, "Music/{artist}/{album} ({year?})", "Music/Some Artist/Some Album (2025)"),
        ("albums", make_album_song, "Music/{album_artist}/{album} ({year?})", "Music/Some Artist/Some Album (2025)"),
    ],
)
def test_folder_templates_keep_path_separators(folder_config, kind, make_title, template, expected):
    """`/` survives because each segment is formatted separately, but edge spaces do not."""
    folder_config.folder_templates = {kind: template}
    result = make_title().get_filename(StubMediaInfo(), folder=True)
    assert result == expected
    assert_safe_path(result)


@pytest.mark.parametrize(
    ("kind", "make_title", "template"),
    [
        ("movies", make_movie, "{title} ({year?})"),
        ("series", make_episode, "{title} ({year?})"),
        ("songs", make_song, "{artist} - {album} [{genre?}]"),
        ("albums", make_album_song, "{album_artist} - {album} [{genre?}]"),
    ],
)
def test_folder_names_are_safe_when_conditionals_are_omitted(folder_config, kind, make_title, template):
    folder_config.folder_templates = {kind: template}
    assert_safe_path(make_title().get_filename(StubMediaInfo(), folder=True))


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("Shows/{title}.{year?}/Season.{season}", "Shows/Some.Show/Season.S01"),
        ("Shows/{title} {year?}/Season {season}", "Shows/Some Show/Season S01"),
    ],
)
def test_series_folder_handles_both_separator_styles(folder_config, template, expected):
    folder_config.folder_templates = {"series": template}
    result = make_episode().get_filename(StubMediaInfo(), folder=True)
    assert result == expected
    assert_safe_path(result)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        # A segment with one variable has no gap to judge by, so it follows the whole
        # path's style instead of falling back to a dot.
        ("Music/{artist}/{album} ({year?})", "Music/Some Artist/Some Album (2025)"),
        ("Music/{artist}/{album}.{year?}", "Music/Some.Artist/Some.Album.2025"),
        ("{artist}/{album}", "Some.Artist/Some.Album"),
    ],
)
def test_single_variable_segments_follow_the_whole_path(folder_config, template, expected):
    folder_config.folder_templates = {"songs": template}
    result = make_song().get_filename(StubMediaInfo(), folder=True)
    assert result == expected
    assert_safe_path(result)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{title}.{year}.{quality}", "."),
        ("{title} {year} {quality}", " "),
        ("{title} ({year})", " "),
        ("Movies/{title} ({year?})", " "),
        ("Shows/{title}.{year?}/Season.{season}", "."),
        ("{title}", "."),  # nothing between two variables, so scene style wins
    ],
)
def test_detect_spacer(template, expected):
    assert detect_spacer(template) == expected


@pytest.mark.parametrize(
    ("series_template", "expected"),
    [
        # Deriving a show folder strips the episode tokens, so the template ends on a
        # separator once {episode_name?} goes too.
        ("{title}.{year?}.{season_episode}.{episode_name?}", "Some.Show.S01"),
        ("{title} {year?} {season_episode} {episode_name?}", "Some Show S01"),
    ],
)
def test_series_folder_derived_from_output_template(folder_config, series_template, expected):
    """With no folder template set, the show folder comes from the file template."""
    folder_config.output_template = {"series": series_template}
    result = make_episode().get_filename(StubMediaInfo(), folder=True)
    assert result == expected
    assert_safe_path(result)


@pytest.mark.parametrize("make_title", [make_movie, make_episode, make_song, make_album_song])
def test_folder_fallbacks_need_no_template(folder_config, make_title):
    assert_safe_path(make_title().get_filename(StubMediaInfo(), folder=True))
