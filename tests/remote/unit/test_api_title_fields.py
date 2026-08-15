"""The title fields that cross the remote wire: air_date, absolute, anime and daily.

Covers serialize_title's conditional keys, the service-class ANIME/DAILY stamp, and the
serialize_title / title_to_meta round trips through remote_service._build_title.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any, Dict, List, Optional

import pytest
from langcodes import Language

from unshackle.commands.dl import dl
from unshackle.core.api import handlers
from unshackle.core.api.handlers import _stamp_service_flags, serialize_title
from unshackle.core.remote_service import _build_title
from unshackle.core.titles.episode import Episode
from unshackle.core.titles.movie import Movie

pytestmark = pytest.mark.unit


class _FakeSvc:
    pass


def _ep(**overrides: Any) -> Episode:
    base: Dict[str, Any] = dict(
        id_="ep-0001",
        service=_FakeSvc,
        title="My Show",
        season=1,
        number=1,
        name="Pilot",
        year=2024,
        language=None,
    )
    base.update(overrides)
    return Episode(**base)


def _movie(**overrides: Any) -> Movie:
    base: Dict[str, Any] = dict(id_="movie-0001", service=_FakeSvc, name="Film", year=2024, language=Language.get("en"))
    base.update(overrides)
    return Movie(**base)


# ---------- serialize_title: the new conditional keys ----------


NEW_KEYS = ("air_date", "absolute", "daily", "anime")


def test_a_plain_episode_gains_no_new_keys() -> None:
    d = serialize_title(_ep())
    assert not [k for k in NEW_KEYS if k in d]


def test_a_plain_movie_gains_no_new_keys() -> None:
    d = serialize_title(_movie())
    assert not [k for k in NEW_KEYS if k in d]


def test_air_date_is_serialized_as_an_iso_string() -> None:
    assert serialize_title(_ep(air_date=date(2026, 8, 11)))["air_date"] == "2026-08-11"


def test_an_unparseable_air_date_is_serialized_as_text() -> None:
    assert serialize_title(_ep(air_date="soon"))["air_date"] == "soon"


def test_absolute_is_serialized() -> None:
    assert serialize_title(_ep(absolute=57))["absolute"] == 57


@pytest.mark.parametrize("value", [True, False])
def test_per_title_daily_and_anime_are_serialized_both_ways(value: bool) -> None:
    episode = _ep()
    episode.daily = value
    episode.anime = value
    d = serialize_title(episode)
    assert (d["daily"], d["anime"]) == (value, value)


def test_a_movie_carries_the_anime_flag_but_never_daily() -> None:
    movie = _movie()
    movie.anime = True
    d = serialize_title(movie)
    assert d["anime"] is True
    assert "daily" not in d


# ---------- the service-class ANIME/DAILY stamp ----------


class _AnimeDailySvc:
    ANIME = True
    DAILY = True


class _PlainSvc:
    pass


def test_the_stamp_fills_both_flags_from_the_service_class() -> None:
    d = _stamp_service_flags(serialize_title(_ep()), _AnimeDailySvc())
    assert (d["anime"], d["daily"]) == (True, True)


def test_the_stamp_never_overrides_a_per_title_false() -> None:
    episode = _ep()
    episode.anime = False
    episode.daily = False
    d = _stamp_service_flags(serialize_title(episode), _AnimeDailySvc())
    assert (d["anime"], d["daily"]) == (False, False)


def test_a_plain_service_stamps_nothing() -> None:
    d = _stamp_service_flags(serialize_title(_ep()), _PlainSvc())
    assert not [k for k in NEW_KEYS if k in d]


def test_a_movie_is_never_stamped_daily() -> None:
    d = _stamp_service_flags(serialize_title(_movie()), _AnimeDailySvc())
    assert d["anime"] is True
    assert "daily" not in d


class _FakeService:
    ANIME = True
    DAILY = True

    def __init__(self, titles: List[Episode]) -> None:
        self._titles = titles

    def get_titles(self) -> List[Episode]:
        return self._titles


def test_list_titles_stamps_the_service_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "validate_service", lambda tag, request=None: "FAKE")
    monkeypatch.setattr(handlers, "setup_list_service", lambda *a, **kw: _FakeService([_ep()]))
    response = asyncio.run(handlers.list_titles_handler({"service": "FAKE", "title_id": "t1"}))
    title = json.loads(response.body)["titles"][0]
    assert (title["anime"], title["daily"]) == (True, True)


# ---------- round trips through _build_title ----------


def _round_trip(episode: Episode, stamp: Optional[Any] = None) -> Episode:
    info = serialize_title(episode)
    if stamp is not None:
        info = _stamp_service_flags(info, stamp)
    return _build_title(info, "FAKE", "fallback-id")


def test_serialize_round_trip_keeps_the_new_fields() -> None:
    episode = _ep(air_date=date(2026, 8, 11), absolute=57)
    episode.daily = True
    episode.anime = True
    rebuilt = _round_trip(episode)
    assert rebuilt.air_date == date(2026, 8, 11)
    assert rebuilt.absolute == 57
    assert (rebuilt.daily, rebuilt.anime) == (True, True)


def test_serialize_round_trip_leaves_unset_flags_inheriting() -> None:
    rebuilt = _round_trip(_ep())
    assert rebuilt.air_date is None
    assert rebuilt.absolute is None
    assert (rebuilt.daily, rebuilt.anime) == (None, None)


def test_the_stamped_service_flags_survive_the_round_trip() -> None:
    rebuilt = _round_trip(_ep(), stamp=_AnimeDailySvc())
    assert (rebuilt.daily, rebuilt.anime) == (True, True)


def test_a_per_title_false_survives_the_round_trip() -> None:
    episode = _ep()
    episode.daily = False
    rebuilt = _round_trip(episode)
    assert rebuilt.daily is False


def test_a_movie_round_trip_keeps_the_anime_flag() -> None:
    movie = _movie()
    movie.anime = True
    rebuilt = _build_title(serialize_title(movie), "FAKE", "fallback-id")
    assert isinstance(rebuilt, Movie)
    assert rebuilt.anime is True


# ---------- round trips through title_to_meta (the export/import path) ----------


def test_title_to_meta_omits_the_new_keys_for_a_plain_episode() -> None:
    meta = dl.title_to_meta(_ep())
    assert not [k for k in ("absolute", "daily", "anime") if k in meta]


def test_title_to_meta_round_trip_keeps_the_new_fields() -> None:
    episode = _ep(air_date=date(2026, 8, 11), absolute=57)
    episode.daily = True
    episode.anime = True
    rebuilt = _build_title(dl.title_to_meta(episode), "FAKE", "fallback-id")
    assert rebuilt.air_date == date(2026, 8, 11)
    assert rebuilt.absolute == 57
    assert (rebuilt.daily, rebuilt.anime) == (True, True)


def test_title_to_meta_round_trip_leaves_unset_flags_inheriting() -> None:
    rebuilt = _build_title(dl.title_to_meta(_ep()), "FAKE", "fallback-id")
    assert rebuilt.absolute is None
    assert (rebuilt.daily, rebuilt.anime) == (None, None)


def test_title_to_meta_round_trip_keeps_the_movie_anime_flag() -> None:
    movie = _movie()
    movie.anime = True
    rebuilt = _build_title(dl.title_to_meta(movie), "FAKE", "fallback-id")
    assert rebuilt.anime is True
