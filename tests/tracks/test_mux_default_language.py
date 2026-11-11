from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import pytest
from langcodes import Language

from unshackle.core.config import config
from unshackle.core.tracks import Audio, Subtitle, Tracks


@pytest.mark.parametrize(
    ("languages", "preferred", "expected"),
    [
        (["en-GB", "en-US"], "en-US", 1),
        # matched against every track at once, so the string-equal tag wins over a bare en
        (["en", "en-US"], "en-US", 1),
        (["en-GB", "fr"], "en-US", None),
        (["fr", "en-GB"], "en", 1),
        (["en-US"], None, None),
    ],
)
def test_first_match_index(languages: list[str], preferred: Optional[str], expected: Optional[int]) -> None:
    tracks = [
        Audio(id_=f"a{i}", url=f"https://x/{i}", language=lang, codec=Audio.Codec.AAC, bitrate=1)
        for i, lang in enumerate(languages)
    ]
    assert Tracks.first_match_index(tracks, preferred) == expected


def subtitle(index: int, language: str, kind: str) -> Subtitle:
    return Subtitle(
        id_=f"s{index}",
        url=f"https://x.example/{index}",
        language=language,
        codec=Subtitle.Codec.WebVTT,
        forced=kind == "forced",
        sdh=kind == "sdh",
    )


@pytest.mark.parametrize(
    ("language", "types", "expected"),
    [
        ("pl", [], 0),
        ("pl", ["normal", "sdh", "forced"], 1),
        ("pl", ["forced"], 0),
        ("en", ["sdh", "normal"], 3),
        ("pl", ["sdh"], 0),
        ("fr", ["normal"], None),
        (None, ["normal"], 1),
        (None, ["sdh", "normal"], 3),
        (None, [], None),
    ],
)
def test_default_subtitle_index(language: Optional[str], types: list[str], expected: Optional[int]) -> None:
    kinds = [("pl", "forced"), ("pl", "normal"), ("en", "normal"), ("en", "sdh")]
    subtitles = [subtitle(i, lang, kind) for i, (lang, kind) in enumerate(kinds)]
    assert Tracks.default_subtitle_index(subtitles, language, types) == expected


@pytest.mark.parametrize(
    ("audio", "expected"),
    [("en", 1), ("ar", 0), ("fr", 2), (None, 2)],
)
def test_a_forced_default_with_no_language_follows_the_audio(audio: Optional[str], expected: int) -> None:
    kinds = [("ar", "forced"), ("en", "forced"), ("en", "normal")]
    subtitles = [subtitle(i, lang, kind) for i, (lang, kind) in enumerate(kinds)]
    audio_language = Language.get(audio) if audio else None
    assert Tracks.default_subtitle_index(subtitles, None, ["forced", "normal"], audio_language) == expected


def downloaded_tracks(tmp_path: Path, audio: list[tuple[str, bool]], subtitles: list[tuple[str, str]]) -> Tracks:
    tracks = Tracks()
    for i, (lang, original) in enumerate(audio):
        track = Audio(id_=f"a{i}", url=f"https://x.example/a{i}", language=lang, is_original_lang=original)
        tracks.add(track)
    for i, (lang, kind) in enumerate(subtitles):
        tracks.add(subtitle(i, lang, kind))
    for track in tracks:
        track.path = tmp_path / f"{track.id}.bin"
        track.path.write_bytes(b"\x00")
    return tracks


def subtitle_defaults(args: list[str], count: int) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args) if arg == "--default-track"][-count:]


def test_mux_reads_default_subtitle_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mux_args: Callable[..., list[str]],
) -> None:
    monkeypatch.setattr(config, "muxing", {"default_subtitle_type": ["full", "CC", "Normal"]})
    tracks = downloaded_tracks(tmp_path, [("en", True)], [("pl", "forced"), ("pl", "normal"), ("en", "sdh")])

    with caplog.at_level(logging.WARNING):
        args = mux_args(tracks)

    assert subtitle_defaults(args, 3) == ["0:False", "0:False", "0:True"]
    assert "'full' is not a subtitle type" in caplog.text


def test_the_forced_default_follows_the_default_audio(tmp_path: Path, mux_args: Callable[..., list[str]]) -> None:
    tracks = downloaded_tracks(tmp_path, [("en", False), ("pl", True)], [("en", "forced"), ("pl", "forced")])

    assert subtitle_defaults(mux_args(tracks), 2) == ["0:False", "0:True"]
