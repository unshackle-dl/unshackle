"""Unit tests for multi-part episode support across the REST/API layer.

Covers serialize_title's conditional `part` key, the list-tracks wanted filter
routing through Episode.matches_wanted, the discrete season/episode/part request
fields, and download_manager's already-internal wanted sniff.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

import pytest

from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError
from unshackle.core.api.handlers import serialize_title
from unshackle.core.titles.episode import Episode

pytestmark = pytest.mark.unit


class _FakeSvc:
    pass


def _ep(season: int = 1, number: int = 1, part: Optional[int] = None, **overrides: Any) -> Episode:
    base: Dict[str, Any] = dict(
        id_=f"ep-{season}-{number}-{part or 0}",
        service=_FakeSvc,
        title="My Show",
        season=season,
        number=number,
        name="Pilot",
        year=2024,
        language=None,
    )
    base.update(overrides)
    return Episode(part=part, **base)


class _FakeTracks:
    videos: List[Any] = []
    audio: List[Any] = []
    subtitles: List[Any] = []


class _FakeService:
    def __init__(self, titles: List[Episode]) -> None:
        self._titles = titles

    def get_titles(self) -> List[Episode]:
        return self._titles

    def get_tracks(self, title: Episode) -> _FakeTracks:
        return _FakeTracks()


def _run_list_tracks(monkeypatch: pytest.MonkeyPatch, titles: List[Episode], **data: Any) -> Dict[str, Any]:
    """Drive list_tracks_handler end to end with the service layer stubbed out."""
    monkeypatch.setattr(handlers, "validate_service", lambda tag, request=None: "FAKE")
    monkeypatch.setattr(handlers, "setup_list_service", lambda *a, **kw: _FakeService(titles))
    payload = {"service": "FAKE", "title_id": "t1", **data}
    try:
        response = asyncio.run(handlers.list_tracks_handler(payload))
    except APIError as e:
        # routes.py turns this into the HTTP error body; keep the fields the tests assert on
        return {"status": e.http_status, "error_code": e.error_code, "details": e.details}
    return json.loads(response.body)


def _selected(result: Dict[str, Any]) -> List[str]:
    """Selection-syntax keys of whatever the handler returned, single or multi."""
    entries = result["episodes"] if "episodes" in result else [{"title": result["title"]}]
    keys = []
    for entry in entries:
        t = entry["title"]
        suffix = f".{t['part']}" if "part" in t else ""
        keys.append(f"{t['season']}x{t['number']}{suffix}")
    return keys


# ---------- serialize_title: the JSON contract ----------


def test_partless_episode_json_has_no_part_key() -> None:
    d = serialize_title(_ep(part=None))
    assert "part" not in d
    assert list(d.keys()) == [
        "type",
        "name",
        "id",
        "language",
        "description",
        "date",
        "cover_url",
        "year",
        "series_title",
        "season",
        "number",
    ]
    assert d["name"] == "Pilot"


def test_partful_episode_json_carries_part_last() -> None:
    d = serialize_title(_ep(part=2))
    assert d["part"] == 2
    # additive: `part` is appended, every pre-existing key keeps its position
    assert list(d.keys())[:11] == list(serialize_title(_ep(part=None)).keys())
    assert list(d.keys())[11] == "part"


def test_part_stays_out_of_the_name_field() -> None:
    """`name` is a reconstruction input for _build_title, not a display string."""
    assert serialize_title(_ep(part=2))["name"] == "Pilot"
    assert serialize_title(_ep(part=3, name=None))["name"] == "Episode 01"


def test_remote_build_title_round_trip() -> None:
    from unshackle.core.remote_service import _build_title

    rebuilt = _build_title(serialize_title(_ep(1, 4, 2)), "FAKE", "fallback-id")
    assert (rebuilt.season, rebuilt.number, rebuilt.part, rebuilt.name) == (1, 4, 2, "Pilot")

    partless = _build_title(serialize_title(_ep(1, 4, None)), "FAKE", "fallback-id")
    assert partless.part is None


def test_movie_json_unaffected() -> None:
    from langcodes import Language

    from unshackle.core.titles.movie import Movie

    d = serialize_title(Movie(id_="movie-0001", service=_FakeSvc, name="Film", year=2024, language=Language.get("en")))
    assert "part" not in d


# ---------- list_tracks_handler: the REST wanted filter ----------


@pytest.fixture
def split_titles() -> List[Episode]:
    """E1 split into three parts, E2 unsplit."""
    return [_ep(1, 1, 1), _ep(1, 1, 2), _ep(1, 1, 3), _ep(1, 2, None)]


def test_rest_bare_episode_key_selects_every_part(monkeypatch, split_titles) -> None:
    result = _run_list_tracks(monkeypatch, split_titles, wanted="S01E01")
    assert _selected(result) == ["1x1.1", "1x1.2", "1x1.3"]


def test_rest_part_key_selects_exactly_one(monkeypatch, split_titles) -> None:
    result = _run_list_tracks(monkeypatch, split_titles, wanted="S01E01.2")
    assert _selected(result) == ["1x1.2"]


def test_rest_negative_part_key_excludes_only_that_part(monkeypatch, split_titles) -> None:
    result = _run_list_tracks(monkeypatch, split_titles, wanted=["S01", "-S01E01.2"])
    assert _selected(result) == ["1x1.1", "1x1.3", "1x2"]


def test_rest_partless_episode_unchanged(monkeypatch, split_titles) -> None:
    result = _run_list_tracks(monkeypatch, split_titles, wanted="S01E02")
    assert _selected(result) == ["1x2"]
    assert "part" not in result["title"]


def test_rest_part_of_unsplit_episode_selects_nothing(monkeypatch, split_titles) -> None:
    result = _run_list_tracks(monkeypatch, split_titles, wanted="S01E02.1")
    assert result["status"] == 404


# ---------- discrete season/episode/part request fields ----------


def test_discrete_fields_without_part_select_every_part(monkeypatch, split_titles) -> None:
    result = _run_list_tracks(monkeypatch, split_titles, season=1, episode=1)
    assert _selected(result) == ["1x1.1", "1x1.2", "1x1.3"]


def test_discrete_fields_with_part_select_one(monkeypatch, split_titles) -> None:
    result = _run_list_tracks(monkeypatch, split_titles, season=1, episode=1, part=3)
    assert _selected(result) == ["1x1.3"]


def test_discrete_part_accepts_string(monkeypatch, split_titles) -> None:
    result = _run_list_tracks(monkeypatch, split_titles, season=1, episode=1, part="3")
    assert _selected(result) == ["1x1.3"]


def test_discrete_no_match_error_reports_the_part_qualified_key(monkeypatch, split_titles) -> None:
    result = _run_list_tracks(monkeypatch, split_titles, season=1, episode=1, part=9)
    assert result["status"] == 404
    assert result["details"]["wanted"] == "1x1.9"


def test_discrete_partless_error_key_unchanged(monkeypatch, split_titles) -> None:
    result = _run_list_tracks(monkeypatch, split_titles, season=9, episode=9)
    assert result["details"]["wanted"] == "9x9"


def test_part_is_a_transport_key_not_a_service_option() -> None:
    """`part` addresses the title, so it must never reach a service's own --part option."""
    assert "part" in handlers.LIST_HANDLER_TRANSPORT_KEYS
    assert "part" in handlers.SESSION_TRANSPORT_KEYS


# ---------- ordering ----------


def test_rest_multi_episode_order_is_stable_across_parts(monkeypatch) -> None:
    titles = [_ep(1, 2, None), _ep(1, 1, 3), _ep(1, 1, 1), _ep(1, 1, 2)]
    result = _run_list_tracks(monkeypatch, titles, wanted="S01")
    assert _selected(result) == ["1x1.1", "1x1.2", "1x1.3", "1x2"]


# ---------- download_manager: already-internal wanted sniff ----------


_SNIFF = r"^!?\d+x\d+(\.\d+)?$"


@pytest.mark.parametrize(
    "wanted, internal",
    [
        (["1x1"], True),  # today's pre-parsed form
        (["1x1", "1x2"], True),
        (["1x1.2"], True),  # part key
        (["!1x1.2"], True),  # negative part key
        (["1x1", "!1x1.2"], True),
        (["S01E01"], False),
        (["s1e1.2"], False),
        (["S01"], False),
        (["1x1", "S01E02"], False),
    ],
)
def test_download_manager_sniff_regex(wanted: List[str], internal: bool) -> None:
    needs_conversion = any(not re.match(_SNIFF, w) for w in wanted)
    assert needs_conversion is (not internal)


def test_sniff_regex_matches_the_one_in_download_manager() -> None:
    """Guard the copy above against drift from the production regex."""
    from pathlib import Path

    import unshackle.core.api.download_manager as dm

    source = Path(dm.__file__).read_text(encoding="utf8")
    assert f'needs_conversion = any(not re.match(r"{_SNIFF}", w) for w in wanted_raw)' in source


def test_perform_download_skips_reconversion_for_preparsed_part_keys(monkeypatch) -> None:
    """A pre-parsed list with part and negative keys must not reach parse_tokens."""
    from unshackle.core.api.download_manager import _perform_download
    from unshackle.core.utils.click_types import SeasonRange

    calls = []
    monkeypatch.setattr(SeasonRange, "parse_tokens", lambda self, *t: calls.append(t) or [])

    params: Dict[str, Any] = {"wanted": ["1x1", "1x1.2", "!1x1.3"]}
    with pytest.raises(BaseException):
        # blows up later on the unknown service; the wanted block runs first
        _perform_download("job-1", "NoSuchServiceXYZ", "t1", params)

    assert calls == []
    assert params["wanted"] == ["1x1", "1x1.2", "!1x1.3"]


def test_perform_download_still_converts_cli_style_tokens(monkeypatch) -> None:
    from unshackle.core.api.download_manager import _perform_download
    from unshackle.core.utils.click_types import SeasonRange

    calls = []
    monkeypatch.setattr(SeasonRange, "parse_tokens", lambda self, *t: calls.append(t) or ["1x1"])

    params: Dict[str, Any] = {"wanted": "S01E01"}
    with pytest.raises(BaseException):
        _perform_download("job-2", "NoSuchServiceXYZ", "t1", params)

    assert calls == [("S01E01",)]
    assert params["wanted"] == ["1x1"]


# ---------- CLI / REST agreement ----------


def test_dl_and_handlers_share_the_one_match_helper() -> None:
    """Both filters must route through Episode.matches_wanted, or they drift."""
    from pathlib import Path

    import unshackle.commands.dl as dl_module

    dl_source = Path(dl_module.__file__).read_text(encoding="utf8")
    handlers_source = Path(handlers.__file__).read_text(encoding="utf8")

    assert "matches_wanted(wanted)" in dl_source
    assert "matches_wanted(wanted)" in handlers_source
    # no inline key compare left behind on either side
    inline = re.compile(r"f\"\{title\.season\}x\{title\.number\}\"\s*\n?\s*(?:in|not in)\s+wanted")
    assert not inline.search(dl_source)
    assert not inline.search(handlers_source)


@pytest.mark.parametrize(
    "wanted_tokens",
    [
        ("S01E01",),
        ("S01E01.2",),
        ("S01",),
        ("S01", "-S01E01.2"),
        ("S01", "-S01E01"),
        ("S01E01.1-S01E01.3",),
        ("S01E02",),
    ],
)
def test_rest_filter_agrees_with_cli_filter(monkeypatch, split_titles, wanted_tokens) -> None:
    from unshackle.core.utils.click_types import SeasonRange

    wanted = SeasonRange().parse_tokens(*wanted_tokens)
    cli_selected = [
        f"{t.season}x{t.number}" + (f".{t.part}" if t.part is not None else "")
        for t in split_titles
        if t.matches_wanted(wanted)
    ]

    result = _run_list_tracks(monkeypatch, split_titles, wanted=list(wanted_tokens))
    rest_selected = [] if result.get("status") == 404 else _selected(result)

    assert sorted(rest_selected) == sorted(cli_selected)
