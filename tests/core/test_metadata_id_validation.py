"""Input validation for the --tmdb / --imdb / --tvdb flags."""

from __future__ import annotations

import click
import pytest

from unshackle.commands.dl import validate_metadata_ids
from unshackle.core.api.handlers import validate_download_parameters
from unshackle.core.config import config


@pytest.fixture(autouse=True)
def _default_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "metadata_providers", [])


def _with_keys(monkeypatch: pytest.MonkeyPatch, **keys: str) -> None:
    for name in ("tmdb_api_key", "tvdb_api_key", "omdb_api_key", "simkl_client_id"):
        monkeypatch.setattr(config, name, keys.get(name, ""))


# ---------- one ID at a time ----------


@pytest.mark.parametrize(
    ("tmdb", "imdb", "tvdb"),
    [(27205, "tt1375666", None), (27205, None, 73871), (None, "tt1375666", 73871), (27205, "tt1375666", 73871)],
)
def test_two_ids_are_refused(tmdb: int | None, imdb: str | None, tvdb: int | None) -> None:
    with pytest.raises(click.UsageError) as exc:
        validate_metadata_ids(tmdb, imdb, tvdb)
    assert "cannot be used together" in str(exc.value)


def test_no_ids_is_fine() -> None:
    assert validate_metadata_ids(None, None, None) is None


def test_imdb_alone_passes_on_the_keyless_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_keys(monkeypatch)
    assert validate_metadata_ids(None, "tt1375666", None) is None


# ---------- the ID must have a provider that can resolve it ----------


def test_tmdb_without_its_key_names_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_keys(monkeypatch)
    with pytest.raises(click.UsageError) as exc:
        validate_metadata_ids(27205, None, None)
    assert "--tmdb" in str(exc.value) and "tmdb_api_key" in str(exc.value)


def test_tvdb_without_its_key_names_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_keys(monkeypatch)
    with pytest.raises(click.UsageError) as exc:
        validate_metadata_ids(None, None, 73871)
    assert "--tvdb" in str(exc.value) and "tvdb_api_key" in str(exc.value)


def test_tmdb_with_its_key_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_keys(monkeypatch, tmdb_api_key="k")
    assert validate_metadata_ids(27205, None, None) is None


def test_a_provider_filtered_out_of_the_order_points_at_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_keys(monkeypatch, tmdb_api_key="k")
    monkeypatch.setattr(config, "metadata_providers", ["imdb", "omdb"])
    with pytest.raises(click.UsageError) as exc:
        validate_metadata_ids(27205, None, None)
    assert "metadata_providers" in str(exc.value)


def test_imdb_is_unresolvable_when_the_order_drops_both_of_its_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_keys(monkeypatch)
    monkeypatch.setattr(config, "metadata_providers", ["tmdb", "tvdb"])
    with pytest.raises(click.UsageError) as exc:
        validate_metadata_ids(None, "tt1375666", None)
    assert "metadata_providers" in str(exc.value)
    assert "imdb" in str(exc.value) and "omdb" in str(exc.value)


def test_one_kind_keeping_the_provider_is_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-kind order can drop tvdb for movies and keep it for tv, so this must not error."""
    _with_keys(monkeypatch, tvdb_api_key="k")
    monkeypatch.setattr(config, "metadata_providers", {"tv": ["tvdb"], "movie": ["tmdb"]})
    assert validate_metadata_ids(None, None, 73871) is None


# ---------- the REST path rejects the same combinations ----------


@pytest.mark.parametrize(
    "payload",
    [
        {"tmdb_id": 27205, "imdb_id": "tt1375666"},
        {"tmdb_id": 27205, "tvdb_id": 73871},
        {"imdb_id": "tt1375666", "tvdb_id": 73871},
    ],
)
def test_api_refuses_two_ids(payload: dict) -> None:
    err = validate_download_parameters(payload)
    assert err and "multiple external IDs" in err


@pytest.mark.parametrize("payload", [{"tmdb_id": 27205}, {"imdb_id": "tt1375666"}, {"tvdb_id": 73871}, {}])
def test_api_accepts_one_id(payload: dict) -> None:
    assert validate_download_parameters(payload) is None


def test_api_still_reports_a_malformed_id_first() -> None:
    err = validate_download_parameters({"tmdb_id": "12345", "imdb_id": "tt1"})
    assert err and "positive integer" in err


# ---------- --anilist stays outside the mutual exclusion ----------


@pytest.mark.parametrize("value", [21, "21", "mal:21"])
def test_anilist_alone_passes_without_any_key(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    _with_keys(monkeypatch)
    assert validate_metadata_ids(None, None, None, value) is None


def test_anilist_pairs_with_one_other_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_keys(monkeypatch)
    assert validate_metadata_ids(None, "tt1375666", None, 21) is None


def test_anilist_does_not_excuse_two_western_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_keys(monkeypatch)
    with pytest.raises(click.UsageError) as exc:
        validate_metadata_ids(27205, "tt1375666", None, 21)
    assert "cannot be used together" in str(exc.value)


@pytest.mark.parametrize("value", ["tt1375666", "anilist:21", "mal:", "abc", "-3"])
def test_a_malformed_anilist_value_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _with_keys(monkeypatch)
    with pytest.raises(click.UsageError) as exc:
        validate_metadata_ids(None, None, None, value)
    assert "--anilist" in str(exc.value)


@pytest.mark.parametrize("payload", [{"anilist_id": 21}, {"anilist_id": "mal:21"}, {"anilist_id": "21"}])
def test_api_accepts_an_anilist_id(payload: dict) -> None:
    assert validate_download_parameters(payload) is None


def test_api_accepts_anilist_alongside_one_western_id() -> None:
    assert validate_download_parameters({"imdb_id": "tt1375666", "anilist_id": 21}) is None


def test_api_still_refuses_two_western_ids_with_anilist() -> None:
    err = validate_download_parameters({"tmdb_id": 27205, "imdb_id": "tt1375666", "anilist_id": 21})
    assert err and "multiple external IDs" in err


@pytest.mark.parametrize("value", [0, -3, True, "mal:", "tt1375666", "anilist:21", 1.5])
def test_api_refuses_a_malformed_anilist_id(value: object) -> None:
    err = validate_download_parameters({"anilist_id": value})
    assert err and "anilist_id" in err


@pytest.mark.parametrize("value", ["0", "mal:0", "²", "21.5"])
def test_a_non_positive_or_non_decimal_anilist_value_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _with_keys(monkeypatch)
    with pytest.raises(click.UsageError) as exc:
        validate_metadata_ids(None, None, None, value)
    assert "--anilist" in str(exc.value)


@pytest.mark.parametrize("payload", [{"anilist_id": "MAL:21"}, {"anilist_id": "007"}, {"anilist_id": " 21 "}])
def test_api_accepts_what_the_cli_accepts(payload: dict) -> None:
    """Both surfaces share parse_anilist_ref, so their accept sets cannot drift."""
    assert validate_download_parameters(payload) is None


@pytest.mark.parametrize("value", ["0", "mal:0", "²"])
def test_api_refuses_what_the_cli_refuses(value: str) -> None:
    err = validate_download_parameters({"anilist_id": value})
    assert err and "anilist_id" in err
