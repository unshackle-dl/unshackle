"""Tests for date-based ("daily"/sports) episode naming via Episode.air_date."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from unshackle.core.config import config
from unshackle.core.titles.episode import Episode


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
        season=2024,
        number=181,
        name="The Big Race",
        year=2024,
    )
    kwargs.update(overrides)
    return Episode(**kwargs)


@pytest.fixture
def reset_template():
    saved = (config.output_template, config.folder_templates, config.folder_template)
    config.output_template = {"series": "{title}.{year?}.{season_episode}.{quality}-{tag}"}
    config.folder_templates = {}
    config.folder_template = ""
    yield config
    config.output_template, config.folder_templates, config.folder_template = saved


def test_air_date_replaces_season_episode_dotted(reset_template):
    ep = make_episode(air_date=date(2024, 6, 30))
    ctx = ep._build_template_context(StubMediaInfo())
    assert ctx["season_episode"] == "2024.06.30"
    assert ctx["season"] == "2024.06.30"
    assert ctx["episode"] == ""
    assert ctx["year"] == ""  # date carries the year
    assert ctx["date"] == "2024-06-30"  # ISO regardless of separator


def test_air_date_uses_space_separator(reset_template):
    reset_template.output_template = {"series": "{title} {season_episode} {quality}-{tag}"}
    ep = make_episode(air_date=date(2024, 6, 30))
    assert ep._build_template_context(StubMediaInfo())["season_episode"] == "2024 06 30"


def test_iso_string_air_date_coerced(reset_template):
    ep = make_episode(air_date="2024-06-30")
    assert isinstance(ep.air_date, date)
    assert ep._build_template_context(StubMediaInfo())["season_episode"] == "2024.06.30"


def test_year_always_dropped_in_file_when_dated(reset_template):
    """The air date is the sole date in the filename; a distinct year is dropped too (deterministic)."""
    ep = make_episode(year=2010, air_date=date(2013, 10, 30))
    ctx = ep._build_template_context(StubMediaInfo())
    assert ctx["year"] == ""
    assert ctx["season_episode"] == "2013.10.30"


def test_yearless_title_takes_year_from_date(reset_template):
    """A title with no year still names cleanly; the date supplies the year."""
    ep = make_episode(year=None, air_date=date(2026, 6, 28))
    ctx = ep._build_template_context(StubMediaInfo())
    assert ctx["year"] == ""
    assert ctx["season_episode"] == "2026.06.28"


def test_datetime_normalized_to_date(reset_template):
    """A datetime (subclass of date) must not leak its time into the {date} token."""
    ep = make_episode(air_date=datetime(2024, 6, 30, 13, 0, 0))
    assert type(ep.air_date) is date
    assert ep._build_template_context(StubMediaInfo())["date"] == "2024-06-30"


def test_no_air_date_is_unchanged(reset_template):
    """Regression: without air_date, naming is exactly as before (presentation-only field)."""
    ep = make_episode()
    ctx = ep._build_template_context(StubMediaInfo())
    assert ctx["season_episode"] == "S2024E181"
    assert ctx["season"] == "S2024"
    assert ctx["episode"] == "E181"
    assert ctx["year"] == 2024
    assert ctx["date"] == ""


def test_dated_folder_groups_by_year(reset_template):
    reset_template.folder_templates = {}
    reset_template.folder_template = ""
    folder = make_episode(air_date=date(2024, 6, 30)).get_filename(StubMediaInfo(), folder=True)
    assert "2024" in folder
    assert "S2024" not in folder  # season folder is the year, not the faked season
