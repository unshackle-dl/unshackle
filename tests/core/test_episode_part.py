"""Tests for multi-part episodes: Episode.part, its naming, sorting and -w matching."""

from __future__ import annotations

import pytest

from unshackle.core.config import config
from unshackle.core.titles.episode import Episode, Series
from unshackle.core.utils.click_types import SeasonRange


class DummyService:
    pass


class StubMediaInfo:
    """Minimal MediaInfo stand-in: the template context only reads track lists."""

    video_tracks: list = []
    audio_tracks: list = []


def make_episode(**overrides) -> Episode:
    kwargs = dict(
        id_="ep-0001",
        service=DummyService,
        title="The Show",
        season=1,
        number=1,
        name="The Reckoning",
    )
    kwargs.update(overrides)
    return Episode(**kwargs)


@pytest.fixture
def reset_template():
    saved = (config.output_template, config.folder_templates, config.folder_template, config.tag)
    config.output_template = {"series": "{title}.{season_episode}.{quality?}.{source}-{tag}"}
    config.folder_templates = {}
    config.folder_template = ""
    config.tag = "GROUP"
    yield config
    config.output_template, config.folder_templates, config.folder_template, config.tag = saved


def test_part_defaults_to_none():
    assert make_episode().part is None


def test_digit_string_part_coerces():
    assert make_episode(part="2").part == 2


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_part_rejected(bad):
    with pytest.raises(ValueError):
        make_episode(part=bad)


def test_non_int_part_rejected():
    with pytest.raises(TypeError):
        make_episode(part="x")


def test_bool_part_rejected():
    """bool is an int subclass; True would render as '.True' in keys and filenames."""
    with pytest.raises(TypeError):
        make_episode(part=True)


def test_old_cache_entry_reads_as_part_less():
    """jsonpickle restores by populating __dict__; an older entry has no 'part' key."""
    restored = Episode.__new__(Episode)
    restored.__dict__.update(title="The Show", season=1, number=1, name=None, year=None)
    assert restored.part is None


def test_str_is_unchanged_without_a_part():
    without = make_episode()
    assert str(without) == "The Show S01E01 The Reckoning"


def test_str_shows_the_selection_syntax():
    assert str(make_episode(part=2)) == "The Show S01E01.2 The Reckoning"


def test_str_tells_dated_parts_apart():
    # dated content renders the air date instead of SxxExx, so without the suffix both
    # parts print the same string while their filenames differ
    assert str(make_episode(part=2, air_date="2024-05-06")) == "The Show 2024.05.06.2 The Reckoning"
    assert str(make_episode(air_date="2024-05-06")) == "The Show 2024.05.06 The Reckoning"


def test_tree_label_carries_the_part_on_dated_content():
    series = Series([make_episode(part=1, air_date="2024-05-06"), make_episode(part=2, air_date="2024-05-06")])
    labels = [node.label for node in series.tree(verbose=True).children[0].children]
    assert labels == [
        "[bold]2024.05.06.1.[/] [bright_black]The Reckoning",
        "[bold]2024.05.06.2.[/] [bright_black]The Reckoning",
    ]


def test_tree_label_carries_the_part():
    series = Series([make_episode(part=1), make_episode(part=2)])
    labels = [node.label for node in series.tree(verbose=True).children[0].children]
    assert labels == ["[bold]1.1.[/] [bright_black]The Reckoning", "[bold]1.2.[/] [bright_black]The Reckoning"]


def test_series_sorts_parts_within_their_episode():
    series = Series(
        [
            make_episode(number=2),
            make_episode(number=1, part=2),
            make_episode(number=1),
            make_episode(number=1, part=1),
        ]
    )
    assert [(e.number, e.part) for e in series] == [(1, None), (1, 1), (1, 2), (2, None)]


def test_filename_is_unchanged_without_a_part(reset_template):
    assert make_episode().get_filename(StubMediaInfo()) == "The.Show.S01E01.DummyService-GROUP"


def test_dotted_template_folds_the_part_after_the_episode(reset_template):
    name = make_episode(part=2).get_filename(StubMediaInfo())
    assert name == "The.Show.S01E01.Part.2.DummyService-GROUP"
    assert name.endswith("-GROUP")  # group tag stays last


def test_spaced_template_uses_spaces(reset_template):
    reset_template.output_template = {"series": "{title} {season_episode} {quality?} {source}-{tag}"}
    assert make_episode(part=2).get_filename(StubMediaInfo()) == "The Show S01E01 Part 2 DummyService-GROUP"


def test_template_context_leaves_the_season_token_alone(reset_template):
    ctx = make_episode(part=2).build_template_context(StubMediaInfo())
    assert ctx["season"] == "S01"
    assert ctx["episode"] == "E01.Part.2"
    assert ctx["season_episode"] == "S01E01.Part.2"
    assert ctx["part"] == 2


def test_part_token_is_empty_without_a_part(reset_template):
    assert make_episode().build_template_context(StubMediaInfo())["part"] == ""


def test_parts_share_one_season_folder(reset_template):
    # {season_episode} in a folder template would otherwise carry the part into the path
    reset_template.folder_template = "{title}/{season_episode}"
    folders = {make_episode(part=p).get_filename(StubMediaInfo(), folder=True) for p in (1, 2)}
    assert folders == {"The.Show/S01E01"}


def test_parts_share_one_derived_season_folder(reset_template):
    folders = {make_episode(part=p).get_filename(StubMediaInfo(), folder=True) for p in (1, 2)}
    assert len(folders) == 1
    assert folders == {make_episode().get_filename(StubMediaInfo(), folder=True)}


# the worked key scheme from the design: (token, part-less E1 selected, E1 part 2 selected)
MATCH_TABLE = [
    (("S01",), True, True),
    (("S01E01",), True, True),
    (("S01E01.2",), False, True),
    (("S01E01.1-S01E01.3",), False, True),
    (("S01", "-S01E01.2"), True, False),
    (("S01", "-S01E01"), False, False),
    (("S01E02",), False, False),
]


@pytest.mark.parametrize("tokens,wants_plain,wants_part2", MATCH_TABLE)
def test_matches_wanted_table(tokens, wants_plain, wants_part2):
    wanted = SeasonRange().parse_tokens(*tokens)
    assert make_episode().matches_wanted(wanted) is wants_plain
    assert make_episode(part=2).matches_wanted(wanted) is wants_part2


def test_a_part_exclusion_leaves_the_other_parts_alone():
    wanted = SeasonRange().parse_tokens("S01", "-S01E01.2")
    assert make_episode(part=1).matches_wanted(wanted) is True
    assert make_episode(part=3).matches_wanted(wanted) is True


def test_asking_for_a_part_of_an_unsplit_episode_selects_nothing():
    assert make_episode().matches_wanted(SeasonRange().parse_tokens("S01E01.2")) is False
