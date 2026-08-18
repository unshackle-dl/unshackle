"""`-w`/`--wanted` track selection for music.

A song shares the episode key space, its disc reading as the season and its track as the
episode. These check the whole path a user takes: the tokens they type, the keys the
click type makes of them, and the titles the download loop keeps.
"""

from __future__ import annotations

import click
import pytest

from unshackle.commands.dl import title_wanted
from unshackle.core.titles.movie import Movie
from unshackle.core.titles.music import Song
from unshackle.core.utils.click_types import SeasonRange


class DummyService:
    pass


def make_song(track: int = 1, disc: int = 1) -> Song:
    return Song(
        id_=f"track-{disc:02}{track:04}",
        service=DummyService,
        name=f"Example Track {track}",
        artist="Example Artist",
        album="Example Album",
        track=track,
        disc=disc,
        year=2025,
        album_artist="Example Artist",
    )


def make_movie() -> Movie:
    return Movie(id_="movie-0001", service=DummyService, name="Example Movie", year=2025)


def select(tokens: str, songs: list[Song]) -> list[int]:
    """Track numbers kept, given what a user types after -w."""
    wanted = SeasonRange().convert(tokens)
    return [song.track for song in songs if title_wanted(song, wanted)]


def single_disc(count: int = 8) -> list[Song]:
    return [make_song(track=n) for n in range(1, count + 1)]


def multi_disc(per_disc: int = 4) -> list[Song]:
    return [make_song(track=n, disc=d) for d in (1, 2) for n in range(1, per_disc + 1)]


def test_a_bare_number_takes_one_track():
    assert select("3", single_disc()) == [3]


def test_a_range_takes_the_tracks_inside_it():
    assert select("2-5", single_disc()) == [2, 3, 4, 5]


def test_a_comma_list_takes_each_track():
    assert select("1,3,7", single_disc()) == [1, 3, 7]


def test_a_leading_minus_removes_a_track_from_a_range():
    assert select("1-5,-3", single_disc()) == [1, 2, 4, 5]


def test_the_disc_form_takes_one_track_of_that_disc():
    songs = multi_disc()
    kept = [(s.disc, s.track) for s in songs if title_wanted(s, SeasonRange().convert("2x3"))]
    assert kept == [(2, 3)]


def test_the_disc_form_ranges_inside_one_disc():
    songs = multi_disc()
    kept = [(s.disc, s.track) for s in songs if title_wanted(s, SeasonRange().convert("2x1-2x3"))]
    assert kept == [(2, 1), (2, 2), (2, 3)]


def test_a_bare_number_on_a_multi_disc_release_is_disc_one():
    """An omitted disc is disc 1, the number the tracklist shows for a one-disc release."""
    songs = multi_disc()
    kept = [(s.disc, s.track) for s in songs if title_wanted(s, SeasonRange().convert("1-2"))]
    assert kept == [(1, 1), (1, 2)]


def test_discs_mix_in_one_selection():
    songs = multi_disc()
    kept = [(s.disc, s.track) for s in songs if title_wanted(s, SeasonRange().convert("1,2x4"))]
    assert kept == [(1, 1), (2, 4)]


def test_a_selection_that_matches_nothing_keeps_no_track():
    assert select("40-45", single_disc()) == []


def test_the_whole_release_downloads_without_a_selection():
    songs = single_disc()
    assert [s.track for s in songs if title_wanted(s, [])] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_a_movie_stays_selected_whatever_the_tokens_are():
    """A movie has no selection key, so -w must not remove it."""
    assert title_wanted(make_movie(), SeasonRange().convert("1-5")) is True


def test_a_range_across_discs_takes_the_rest_of_the_first_disc():
    """Same rule as a season range: the left disc runs to its end before the right one starts."""
    songs = multi_disc()
    kept = [(s.disc, s.track) for s in songs if title_wanted(s, SeasonRange().convert("1x3-2x2"))]
    assert kept == [(1, 3), (1, 4), (2, 1), (2, 2)]


@pytest.mark.parametrize("token", ["1x", "x1", "1x2x3", "1.2"])
def test_bad_music_tokens_fail(token):
    with pytest.raises(click.UsageError):
        SeasonRange().parse_tokens(token)


def test_song_matches_only_its_own_key():
    song = make_song(track=3, disc=2)
    assert song.matches_wanted(["2x3"]) is True
    assert song.matches_wanted(["1x3", "2x4"]) is False
