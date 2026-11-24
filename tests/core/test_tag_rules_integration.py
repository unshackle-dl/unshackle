"""Tests for tag_rules reaching the filename context through Title."""

from __future__ import annotations

import inspect
import types
from typing import Any, Iterator

import pytest
from langcodes import Language

from unshackle.core.config import config
from unshackle.core.titles.movie import Movie


class DummyService:
    pass


class TrackStub:
    """Stand-in for Tracks: truthy and iterable, with the two lists lang_tag reads."""

    def __init__(self, audio: list[Any], subtitles: list[Any]) -> None:
        self.audio = audio
        self.subtitles = subtitles

    def __iter__(self) -> Iterator[Any]:
        return iter([])

    def __bool__(self) -> bool:
        return True


def make_audio(language: str) -> types.SimpleNamespace:
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


def make_movie() -> Movie:
    return Movie(id_="movie-0001", service=DummyService, name="The Film", year=2024, language="en")


@pytest.fixture(autouse=True)
def clean_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "tag", "NOGRP")
    monkeypatch.setattr(config, "tag_rules", [])
    monkeypatch.setattr(config, "language_tags", {})


def context_for(rules: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(config, "tag_rules", rules)
    return make_movie().build_base_template_context(make_media_info(["en"]))


def test_matching_rule_replaces_the_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for([{"when": {"title_type": "movie"}, "tag": "MOVIEGRP"}], monkeypatch)
    assert context["tag"] == "MOVIEGRP"


def test_no_matching_rule_keeps_the_config_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for([{"when": {"title_type": "series"}, "tag": "TVGRP"}], monkeypatch)
    assert context["tag"] == "NOGRP"


def test_no_rules_keeps_the_config_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for([], monkeypatch)
    assert context["tag"] == "NOGRP"


def test_title_type_is_in_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    assert context_for([], monkeypatch)["title_type"] == "movie"


def test_rules_run_after_lang_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rule matching on lang_tag only works if lang_tag is resolved first."""
    monkeypatch.setattr(config, "language_tags", {"rules": [{"audio": "ja", "tag": "SUBBED"}]})
    monkeypatch.setattr(config, "tag_rules", [{"when": {"lang_tag": "SUBBED"}, "tag": "SUBGRP"}])

    movie = make_movie()
    movie.tracks = TrackStub(audio=[types.SimpleNamespace(language=Language.get("ja"))], subtitles=[])  # type: ignore[assignment]
    context = movie.build_base_template_context(make_media_info(["ja"]))

    assert context["lang_tag"] == "SUBBED"
    assert context["tag"] == "SUBGRP"


def test_a_quality_rule_matches_the_built_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the context keys the docs tell users to match on."""
    monkeypatch.setattr(config, "tag_rules", [{"when": {"quality": "2160p", "video": "H.265"}, "tag": "UHDGROUP"}])
    video = types.SimpleNamespace(
        width=3840,
        height=2160,
        format="HEVC",
        scan_type="Progressive",
        other_display_aspect_ratio=["16:9"],
        hdr_format_commercial=None,
        hdr_format=None,
        transfer_characteristics=None,
        transfer_characteristics_original=None,
        frame_rate="23.976",
    )
    media_info = types.SimpleNamespace(video_tracks=[video], audio_tracks=[make_audio("en")])

    context = make_movie().build_base_template_context(media_info)

    assert context["quality"] == "2160p"
    assert context["video"] == "H.265"
    assert context["tag"] == "UHDGROUP"


def test_dl_clears_tag_rules_when_tag_is_given() -> None:
    """Source-level pin: the block lives inside the click-driven dl.__init__."""
    from unshackle.commands.dl import dl

    source = inspect.getsource(dl.__init__)
    block = source.split("if tag:")[1].split("\n\n")[0]

    assert "config.tag = tag" in block
    assert "config.tag_rules = []" in block
