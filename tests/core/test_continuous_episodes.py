"""--continuous-episodes: each season is numbered on from the season before it."""

from __future__ import annotations

from types import SimpleNamespace

from unshackle.commands.dl import dl
from unshackle.core.remote_service import apply_title_fields
from unshackle.core.titles.episode import Episode, Series


class DummyService:
    pass


def episode(season: int, number: int, part: int | None = None) -> Episode:
    return Episode(
        id_=f"ep-{season:02}-{number:02}-{part}",
        service=DummyService,
        title="The Show",
        season=season,
        number=number,
        part=part,
    )


def renumber(series: Series) -> Series:
    stub = SimpleNamespace(log=SimpleNamespace(info=lambda *a, **k: None))
    return dl.apply_continuous_episodes(stub, series)


def numbers(slots: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(t.season, t.number) for t in renumber(Series(episode(s, n) for s, n in slots))]


def test_season_two_starts_after_season_one():
    assert numbers([(1, 1), (1, 2), (1, 3), (1, 4), (2, 1), (2, 2), (3, 1)]) == [
        (1, 1),
        (1, 2),
        (1, 3),
        (1, 4),
        (2, 5),
        (2, 6),
        (3, 7),
    ]


def test_specials_keep_their_numbers():
    assert numbers([(0, 1), (1, 1), (1, 2), (2, 1)]) == [(0, 1), (1, 1), (1, 2), (2, 3)]


def test_already_continuous_season_is_kept():
    assert numbers([(1, 1), (1, 2), (2, 3), (2, 4), (3, 1)]) == [(1, 1), (1, 2), (2, 3), (2, 4), (3, 5)]


def test_episode_zero_keeps_its_number_and_a_second_run_changes_nothing():
    once = numbers([(1, 1), (1, 2), (1, 3), (1, 4), (2, 0), (2, 1), (2, 2)])
    assert once == [(1, 1), (1, 2), (1, 3), (1, 4), (2, 0), (2, 5), (2, 6)]
    assert numbers(once) == once


def test_episode_zero_does_not_make_a_continuous_season_move():
    slots = [(1, 1), (1, 2), (2, 0), (2, 3), (2, 4)]
    assert numbers(slots) == slots


def test_parts_of_one_episode_move_together():
    series = renumber(Series([episode(1, 1), episode(1, 2), episode(2, 1, 1), episode(2, 1, 2)]))
    assert [(t.season, t.number, t.part) for t in series] == [(1, 1, None), (1, 2, None), (2, 3, 1), (2, 3, 2)]


def test_the_rebuilt_series_finds_a_renumbered_episode():
    moved = episode(2, 1)
    series = renumber(Series([episode(1, 1), moved]))
    assert moved in series
    assert series.index(moved) == 1


def test_the_get_tracks_refresh_keeps_the_local_numbers():
    title = episode(2, 3)
    apply_title_fields(title, {"season": 2, "number": 1, "name": "Pilot"})
    assert (title.season, title.number, title.name) == (2, 3, "Pilot")
