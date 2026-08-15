"""Original-language parsing for the metadata a supplied ID resolves."""

from __future__ import annotations

import pytest

from unshackle.commands.dl import parse_language
from unshackle.core.providers.imdb import primary_language as imdb_language
from unshackle.core.providers.omdb import primary_language as omdb_language
from unshackle.core.providers.tvdb import primary_language as tvdb_language


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("ja", "ja"),  # TMDB alpha-2
        ("jpn", "ja"),  # IMDb alpha-3
        ("pt-BR", "pt-BR"),
        (None, None),
        ("", None),
        ("und", None),
        ("xx-not-a-tag", None),
    ],
)
def test_parse_language(tag: str | None, expected: str | None) -> None:
    result = parse_language(tag)
    assert (str(result) if result else None) == expected


def test_imdb_language_takes_the_first_spoken_language() -> None:
    data = {"spokenLanguages": {"spokenLanguages": [{"id": "ja", "text": "Japanese"}, {"id": "en", "text": "English"}]}}
    assert imdb_language(data) == "ja"


def test_imdb_language_is_alpha_2() -> None:
    """IMDb's GraphQL language ids are alpha-2."""
    data = {"spokenLanguages": {"spokenLanguages": [{"id": "ko", "text": "Korean"}]}}
    assert parse_language(imdb_language(data)) is not None
    assert str(parse_language(imdb_language(data))) == "ko"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"spokenLanguages": None},  # search hits can come back with the whole block null
        {"spokenLanguages": {}},
        {"spokenLanguages": {"spokenLanguages": []}},
        {"spokenLanguages": {"spokenLanguages": None}},
    ],
)
def test_imdb_language_missing(data: dict) -> None:
    assert imdb_language(data) is None


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ("Korean", "ko"),
        ("Korean, English", "ko"),  # OMDb lists the main language first
        ("", None),
        ("None", None),  # OMDb's literal placeholder for unknown
        ("Not A Language", None),
    ],
)
def test_omdb_language_from_english_name(names: str, expected: str | None) -> None:
    assert omdb_language({"Language": names}) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"primary_language": "kor"}, "kor"),  # /search
        ({"originalLanguage": "kor"}, "kor"),  # /series/{id}/extended
        ({"primary_language": ""}, None),
        ({}, None),
    ],
)
def test_tvdb_language_key_variants(data: dict, expected: str | None) -> None:
    assert tvdb_language(data) == expected
