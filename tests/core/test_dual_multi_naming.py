"""Tests for {dual}/{multi}/{dubbed} filename variables and dual_multi_mode."""

from __future__ import annotations

import types

import pytest

from unshackle.core.config import config
from unshackle.core.titles.movie import Movie


class DummyService:
    pass


def make_audio(language: str) -> types.SimpleNamespace:
    """Audio track stub carrying just the attributes the context builder reads."""
    return types.SimpleNamespace(
        language=language,
        format="AAC",
        channel_layout=None,
        channellayout_original=None,
        channel_s=2,
        channels=2,
        format_additionalfeatures=None,
        joc=None,
    )


def make_media_info(languages: list[str]) -> types.SimpleNamespace:
    return types.SimpleNamespace(video_tracks=[], audio_tracks=[make_audio(lang) for lang in languages])


def make_movie(language) -> Movie:
    return Movie(id_="movie-0001", service=DummyService, name="The Film", year=2024, language=language)


@pytest.fixture
def strict_mode(monkeypatch):
    monkeypatch.setattr(config, "dual_multi_mode", "strict")


@pytest.fixture
def count_mode(monkeypatch):
    monkeypatch.setattr(config, "dual_multi_mode", "count")


def ctx(language, languages) -> dict:
    return make_movie(language).build_base_template_context(make_media_info(languages))


def test_strict_dual_with_original(strict_mode):
    c = ctx("en", ["en", "es"])
    assert c["dual"] == "DUAL"
    assert c["multi"] == ""
    assert c["dubbed"] == ""


def test_strict_two_dubs_no_original(strict_mode):
    c = ctx("en", ["es", "fr"])
    assert c["dual"] == ""
    assert c["multi"] == ""
    assert c["dubbed"] == ""


def test_strict_regional_variants_collapse(strict_mode):
    c = ctx("en", ["en-US", "en-GB"])
    assert c["dual"] == ""
    assert c["multi"] == ""
    assert c["dubbed"] == ""


def test_strict_multi(strict_mode):
    c = ctx("en", ["es", "fr", "de"])
    assert c["multi"] == "MULTi"
    assert c["dual"] == ""
    assert c["dubbed"] == ""


def test_strict_two_unknown_original(strict_mode):
    c = ctx(None, ["en", "es"])
    assert c["dual"] == ""
    assert c["multi"] == ""
    assert c["dubbed"] == ""


def test_strict_dubbed(strict_mode):
    c = ctx("en", ["es"])
    assert c["dubbed"] == "DUBBED"
    assert c["dual"] == ""
    assert c["multi"] == ""


def test_strict_single_unknown_original_no_dubbed(strict_mode):
    c = ctx(None, ["es"])
    assert c["dubbed"] == ""
    assert c["dual"] == ""
    assert c["multi"] == ""


def test_count_dual_ignores_original(count_mode):
    c = ctx(None, ["en", "es"])
    assert c["dual"] == "DUAL"
    assert c["multi"] == ""
    assert c["dubbed"] == ""


def test_count_multi(count_mode):
    c = ctx(None, ["es", "fr", "de"])
    assert c["multi"] == "MULTi"
    assert c["dual"] == ""
    assert c["dubbed"] == ""


def test_count_never_dubbed(count_mode):
    c = ctx("en", ["es"])
    assert c["dubbed"] == ""
    assert c["dual"] == ""
    assert c["multi"] == ""
