"""Absolute episode numbering: Episode.absolute, the {absolute} token, and the enrich fill."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from unshackle.core import providers
from unshackle.core.config import config
from unshackle.core.titles.episode import Episode, Series


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
    config.output_template = {"series": "{title}.{season_episode}.{absolute}.{source}-{tag}"}
    config.folder_templates = {}
    config.folder_template = ""
    config.tag = "GROUP"
    yield config
    config.output_template, config.folder_templates, config.folder_template, config.tag = saved


def test_absolute_defaults_to_none():
    assert make_episode().absolute is None


def test_int_absolute_is_kept():
    assert make_episode(absolute=27).absolute == 27


def test_digit_string_absolute_coerces():
    assert make_episode(absolute="27").absolute == 27


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_absolute_rejected(bad):
    with pytest.raises(ValueError):
        make_episode(absolute=bad)


@pytest.mark.parametrize("bad", ["x", "1.5", 1.5, []])
def test_non_int_absolute_rejected(bad):
    with pytest.raises(TypeError):
        make_episode(absolute=bad)


def test_bool_absolute_rejected():
    """bool is an int subclass; True would render as '001' in filenames."""
    with pytest.raises(TypeError):
        make_episode(absolute=True)


def test_old_cache_entry_reads_as_absolute_less():
    """jsonpickle restores by populating __dict__; an older entry has no 'absolute' key."""
    restored = Episode.__new__(Episode)
    restored.__dict__.update(title="The Show", season=1, number=1, name=None, year=None)
    assert restored.absolute is None


def test_absolute_token_is_three_wide(reset_template):
    assert make_episode(absolute=7).build_template_context(StubMediaInfo())["absolute"] == "007"


def test_absolute_token_does_not_truncate_past_three_digits(reset_template):
    assert make_episode(absolute=1042).build_template_context(StubMediaInfo())["absolute"] == "1042"


def test_absolute_token_is_empty_without_a_value(reset_template):
    assert make_episode().build_template_context(StubMediaInfo())["absolute"] == ""


def test_filename_renders_the_absolute_number(reset_template):
    assert make_episode(absolute=7).get_filename(StubMediaInfo()) == "The.Show.S01E01.007.DummyService-GROUP"


def test_optional_absolute_token_drops_out(reset_template):
    reset_template.output_template = {"series": "{title}.{season_episode}.{absolute?}.{source}-{tag}"}
    assert make_episode().get_filename(StubMediaInfo()) == "The.Show.S01E01.DummyService-GROUP"
    assert make_episode(absolute=7).get_filename(StubMediaInfo()) == "The.Show.S01E01.007.DummyService-GROUP"


def test_absolute_is_a_known_template_variable(reset_template, recwarn):
    reset_template.validate_output_templates()
    assert not [w for w in recwarn.list if "absolute" in str(w.message)]


def test_str_and_sorting_ignore_the_absolute_number():
    assert str(make_episode(absolute=7)) == "The Show S01E01 The Reckoning"
    series = Series([make_episode(number=2, absolute=1), make_episode(number=1, absolute=2)])
    assert [e.number for e in series] == [1, 2]


class _FakeProvider:
    def __init__(self, mapping: dict, source_order: str = "official") -> None:
        self.mapping = mapping
        self.source_order = source_order
        self.orders: list = []

    def detect_order(self, _id, _keys) -> str:
        return self.source_order

    def get_order_map(self, _id, order, source_order=None) -> dict:
        self.orders.append(order)
        return self.mapping


def fill(
    monkeypatch: pytest.MonkeyPatch, episodes: list, mapping: dict, tvdb_id=73871, provider=True, order="official"
):
    from unshackle.commands.dl import dl

    fake = _FakeProvider(mapping, order) if provider else None
    monkeypatch.setattr(providers, "get_provider", lambda _name: fake)
    logged: list = []
    logger = SimpleNamespace(
        debug=lambda *a, **k: logged.append(("debug", a)),
        info=lambda *a, **k: logged.append(("info", a)),
        warning=lambda *a, **k: logged.append(("warning", a)),
    )
    stub = SimpleNamespace(tvdb_id=tvdb_id, log=logger)
    titles = Series(episodes)
    dl.fill_absolute_numbers(stub, titles, tvdb_id)
    return titles, fake, logged


MAPPING = {(1, 1): (1, 1, "One"), (1, 2): (1, 2, "Two"), (2, 1): (1, 3, "Three")}


def test_fill_populates_missing_absolute_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = [make_episode(season=1, number=1), make_episode(season=1, number=2), make_episode(season=2, number=1)]
    titles, fake, _ = fill(monkeypatch, episodes, MAPPING)
    assert [t.absolute for t in titles] == [1, 2, 3]
    assert fake.orders == ["absolute"]


def test_a_service_already_in_absolute_order_fills_from_its_own_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_order_map returns {} for the same-order case; the numbers are their own fill."""
    episodes = [make_episode(season=1, number=26), make_episode(season=1, number=27)]
    titles, fake, _ = fill(monkeypatch, episodes, {}, order="absolute")
    assert [t.absolute for t in titles] == [26, 27]
    assert fake.orders == []


def test_fill_never_touches_season_or_number(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = [make_episode(season=2, number=1)]
    titles, _, _ = fill(monkeypatch, episodes, MAPPING)
    assert [(t.season, t.number) for t in titles] == [(2, 1)]


def test_a_service_set_absolute_number_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = [make_episode(season=1, number=1, absolute=99), make_episode(season=1, number=2)]
    titles, _, _ = fill(monkeypatch, episodes, MAPPING)
    assert [t.absolute for t in titles] == [99, 2]


def test_an_unmapped_episode_keeps_no_absolute_number(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = [make_episode(season=9, number=9)]
    titles, _, _ = fill(monkeypatch, episodes, MAPPING)
    assert titles[0].absolute is None


def test_fill_skips_silently_without_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = [make_episode(season=1, number=1)]
    titles, _, logged = fill(monkeypatch, episodes, MAPPING, provider=False)
    assert titles[0].absolute is None
    assert [level for level, _ in logged] == ["debug"]


def test_fill_skips_silently_without_a_tvdb_id(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = [make_episode(season=1, number=1)]
    titles, _, logged = fill(monkeypatch, episodes, MAPPING, tvdb_id=None)
    assert titles[0].absolute is None
    assert [level for level, _ in logged] == ["debug"]


def test_fill_skips_silently_when_tvdb_has_no_absolute_order(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = [make_episode(season=1, number=1)]
    titles, _, logged = fill(monkeypatch, episodes, {})
    assert titles[0].absolute is None
    assert [level for level, _ in logged] == ["debug"]
