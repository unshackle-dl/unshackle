"""Selection for --latest-episode / --latest-episodes and its REST API validation."""

from __future__ import annotations

from typing import Optional

import pytest

from unshackle.commands.dl import latest_episode_keys
from unshackle.core.api.handlers import validate_download_parameters
from unshackle.core.titles import Movie, Movies, Series
from unshackle.core.titles.episode import Episode


class Svc:
    pass


def episode(season: int, number: int, part: Optional[int] = None) -> Episode:
    return Episode(id_=f"ep-{season}x{number}.{part}", service=Svc, title="T", season=season, number=number, part=part)


def series() -> Series:
    # out of order on purpose: the Series sorts itself
    return Series(episode(s, n) for s, n in [(2, 1), (1, 1), (2, 3), (1, 2), (2, 2)])


@pytest.mark.parametrize(
    ("latest_episode", "latest_episodes", "expected"),
    [
        (False, None, []),
        (True, None, [(2, 3)]),
        (False, 3, [(2, 1), (2, 2), (2, 3)]),
        (True, 2, [(2, 2), (2, 3)]),
        (False, 99, [(1, 1), (1, 2), (2, 1), (2, 2), (2, 3)]),
    ],
)
def test_the_newest_episodes_are_picked_across_seasons(
    latest_episode: bool, latest_episodes: Optional[int], expected: list[tuple[int, int]]
) -> None:
    assert latest_episode_keys(series(), latest_episode, latest_episodes) == expected


def test_a_split_episode_takes_one_slot() -> None:
    titles = Series([episode(1, 4), episode(1, 5, part=1), episode(1, 5, part=2)])
    assert latest_episode_keys(titles, False, 2) == [(1, 4), (1, 5)]
    assert latest_episode_keys(titles, True, None) == [(1, 5)]


def test_titles_that_are_not_a_series_are_left_alone() -> None:
    movies = Movies([Movie(id_="movie-1", service=Svc, name="M")])
    assert latest_episode_keys(movies, True, 3) == []


@pytest.mark.parametrize("value", [0, -1, True, "3", 1.5])
def test_a_non_positive_or_non_integer_count_is_refused(value: object) -> None:
    assert validate_download_parameters({"latest_episodes": value}) == "latest_episodes must be a positive integer"


def test_a_positive_count_is_accepted() -> None:
    assert validate_download_parameters({"latest_episodes": 3}) is None
