"""The --daily air-date fill: dl.fill_air_dates and the daily_hint gate."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from unshackle.core import providers
from unshackle.core.titles.episode import Episode, Series


class DummyService:
    pass


def make_episode(**overrides) -> Episode:
    kwargs = dict(
        id_="ep-0001",
        service=DummyService,
        title="The Show",
        season=1,
        number=1,
    )
    kwargs.update(overrides)
    return Episode(**kwargs)


class _FakeProvider:
    def __init__(self, episodes: list, source_order: str = "official") -> None:
        self.episodes = episodes
        self.source_order = source_order

    def detect_order(self, _id, _keys) -> str:
        return self.source_order

    def get_episodes(self, _id, _order) -> list:
        return self.episodes


def fill(
    monkeypatch: pytest.MonkeyPatch,
    titles: list,
    episodes: list,
    tvdb_id=73871,
    provider=True,
    daily=True,
    service_daily=False,
):
    from unshackle.commands.dl import dl

    fake = _FakeProvider(episodes) if provider else None
    monkeypatch.setattr(providers, "get_provider", lambda _name: fake)
    logged: list = []
    logger = SimpleNamespace(
        debug=lambda *a, **k: logged.append(("debug", a)),
        info=lambda *a, **k: logged.append(("info", a)),
        warning=lambda *a, **k: logged.append(("warning", a)),
    )
    stub = SimpleNamespace(tvdb_id=tvdb_id, log=logger, daily=daily, service_daily=service_daily)
    stub.daily_hint = lambda title=None: dl.daily_hint(stub, title)
    series = Series(titles)
    dl.fill_air_dates(stub, series, tvdb_id)
    return series, logged


def tvdb_episode(season: int, number: int, aired) -> dict:
    return {"seasonNumber": season, "number": number, "aired": aired}


LISTING = [tvdb_episode(1, 1, "2026-08-10"), tvdb_episode(1, 2, "2026-08-11")]


def test_fill_populates_missing_air_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = [make_episode(number=1), make_episode(number=2)]
    series, logged = fill(monkeypatch, episodes, LISTING)
    assert [t.air_date for t in series] == [date(2026, 8, 10), date(2026, 8, 11)]
    assert [level for level, _ in logged] == ["info"]


def test_a_service_set_air_date_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = [make_episode(number=1, air_date="1999-01-02"), make_episode(number=2)]
    series, _ = fill(monkeypatch, episodes, LISTING)
    assert [t.air_date for t in series] == [date(1999, 1, 2), date(2026, 8, 11)]


def test_fill_never_touches_season_or_number(monkeypatch: pytest.MonkeyPatch) -> None:
    series, _ = fill(monkeypatch, [make_episode(number=2)], LISTING)
    assert [(t.season, t.number) for t in series] == [(1, 2)]


def test_an_unmapped_episode_keeps_no_air_date(monkeypatch: pytest.MonkeyPatch) -> None:
    series, _ = fill(monkeypatch, [make_episode(season=9, number=9)], LISTING)
    assert series[0].air_date is None


@pytest.mark.parametrize("aired", [None, "", "not-a-date", "2026-13-40"])
def test_an_unusable_aired_value_is_skipped(monkeypatch: pytest.MonkeyPatch, aired) -> None:
    series, _ = fill(monkeypatch, [make_episode(number=1)], [tvdb_episode(1, 1, aired)])
    assert series[0].air_date is None


def test_a_pre_1970_date_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    series, _ = fill(monkeypatch, [make_episode(number=1)], [tvdb_episode(1, 1, "1900-01-01")])
    assert series[0].air_date is None


def test_a_future_placeholder_date_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    future = (date.today() + timedelta(days=30)).isoformat()
    series, _ = fill(monkeypatch, [make_episode(number=1)], [tvdb_episode(1, 1, future)])
    assert series[0].air_date is None


def test_tomorrow_still_fills(monkeypatch: pytest.MonkeyPatch) -> None:
    tomorrow = date.today() + timedelta(days=1)
    series, _ = fill(monkeypatch, [make_episode(number=1)], [tvdb_episode(1, 1, tomorrow.isoformat())])
    assert series[0].air_date == tomorrow


def test_a_datetime_stamped_aired_value_keeps_the_date(monkeypatch: pytest.MonkeyPatch) -> None:
    series, _ = fill(monkeypatch, [make_episode(number=1)], [tvdb_episode(1, 1, "2026-08-10T20:00:00Z")])
    assert series[0].air_date == date(2026, 8, 10)


def test_fill_skips_silently_without_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    series, logged = fill(monkeypatch, [make_episode(number=1)], LISTING, provider=False)
    assert series[0].air_date is None
    assert [level for level, _ in logged] == ["debug"]


def test_fill_skips_silently_without_a_tvdb_id(monkeypatch: pytest.MonkeyPatch) -> None:
    series, logged = fill(monkeypatch, [make_episode(number=1)], LISTING, tvdb_id=None)
    assert series[0].air_date is None
    assert [level for level, _ in logged] == ["debug"]


def test_fill_skips_silently_when_tvdb_lists_no_episodes(monkeypatch: pytest.MonkeyPatch) -> None:
    series, logged = fill(monkeypatch, [make_episode(number=1)], [])
    assert series[0].air_date is None
    assert [level for level, _ in logged] == ["debug"]


def test_nothing_is_filled_when_no_title_is_daily(monkeypatch: pytest.MonkeyPatch) -> None:
    series, logged = fill(monkeypatch, [make_episode(number=1)], LISTING, daily=False)
    assert series[0].air_date is None
    assert logged == []


def test_the_service_daily_flag_fills_without_the_cli_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    series, _ = fill(monkeypatch, [make_episode(number=1)], LISTING, daily=False, service_daily=True)
    assert series[0].air_date == date(2026, 8, 10)


def test_a_per_title_daily_false_is_skipped_while_others_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = make_episode(number=1), make_episode(number=2)
    first.daily = False
    series, _ = fill(monkeypatch, [first, second], LISTING, daily=False, service_daily=True)
    assert [t.air_date for t in series] == [None, date(2026, 8, 11)]


def test_the_cli_flag_overrides_a_per_title_daily_false(monkeypatch: pytest.MonkeyPatch) -> None:
    episode = make_episode(number=1)
    episode.daily = False
    series, _ = fill(monkeypatch, [episode], LISTING, daily=True)
    assert series[0].air_date == date(2026, 8, 10)
