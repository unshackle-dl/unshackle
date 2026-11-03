"""Tests for the ``sports`` output and folder template kind of a sport Episode."""

from __future__ import annotations

from datetime import date

import pytest

from unshackle.commands.dl import dl
from unshackle.core.api.handlers import serialize_title
from unshackle.core.config import Config, config
from unshackle.core.remote_service import build_title
from unshackle.core.titles.episode import Episode

SERIES = "{title}.{year?}.{season_episode}.{episode_name?}.{quality?}-{tag?}"
SPORTS = "{title}.{year?}.{date?}.{episode_name}.{quality?}-{tag?}"


class DummyService:
    pass


class StubMediaInfo:
    """Minimal MediaInfo stand-in: the template context only reads track lists."""

    video_tracks: list = []
    audio_tracks: list = []


def make_episode(**overrides) -> Episode:
    kwargs = dict(
        id_="ev-0001",
        service=DummyService,
        title="League",
        season=1,
        number=2,
        name="City Grand Prix Practice One",
        year=2026,
        sport=True,
    )
    kwargs.update(overrides)
    return Episode(**kwargs)


@pytest.fixture
def templates(monkeypatch):
    monkeypatch.setattr(config, "output_template", {"series": SERIES, "sports": SPORTS})
    monkeypatch.setattr(config, "folder_templates", {"series": "{title}.{year?}.{season}", "sports": "{title}.{year?}"})
    monkeypatch.setattr(config, "folder_template", "")
    return config


def filename(episode: Episode, folder: bool = False) -> str:
    return episode.get_filename(StubMediaInfo(), folder=folder, show_service=False)


def test_a_yearly_event_names_the_file_by_year_and_name(templates):
    assert filename(make_episode()).startswith("League.2026.City.Grand.Prix.Practice.One")


def test_a_dated_event_names_the_file_by_dotted_date(templates):
    episode = make_episode(year=2026, name="Home Club vs Away Club", air_date=date(2026, 11, 1))
    assert filename(episode).startswith("League.2026.11.01.Home.Club.vs.Away.Club")


def test_the_sports_folder_groups_by_year(templates):
    assert filename(make_episode(), folder=True) == "League.2026"


def test_the_sports_folder_takes_the_air_year_without_a_year(templates):
    episode = make_episode(year=None, air_date=date(2026, 11, 1))
    assert filename(episode, folder=True) == "League.2026"


def test_a_sport_episode_uses_series_templates_without_a_sports_template(templates):
    templates.output_template = {"series": SERIES}
    templates.folder_templates = {"series": "{title}.{year?}.{season}"}
    sport, plain = make_episode(), make_episode(sport=False)
    assert filename(sport) == filename(plain)
    assert filename(sport).startswith("League.2026.S01E02")
    assert filename(sport, folder=True) == filename(plain, folder=True) == "League.2026.S01"


def test_a_non_sport_episode_ignores_the_sports_template(templates):
    assert filename(make_episode(sport=False)).startswith("League.2026.S01E02")


def test_the_sport_flag_survives_the_remote_round_trip():
    rebuilt = build_title(serialize_title(make_episode()), "TEST", fallback_id="x")
    assert isinstance(rebuilt, Episode) and rebuilt.sport


def test_the_sport_flag_survives_the_export_round_trip():
    rebuilt = build_title(dl.title_to_meta(make_episode()), "TEST", fallback_id="x")
    assert isinstance(rebuilt, Episode) and rebuilt.sport


def test_a_non_sport_episode_sends_no_sport_key():
    assert "sport" not in serialize_title(make_episode(sport=False))


def test_sports_templates_validate_without_warnings(recwarn):
    Config(output_template={"sports": SPORTS, "folder": {"sports": "{title}.{year?}.{date?}"}})
    assert not [str(w.message) for w in recwarn if "template" in str(w.message).lower()]
