"""Run the kind-only filters through the real dl.result() selection loop.

A fake service hands in-memory tracks to the loop, and the run stops at the first
title that reaches ``Tracks.tree``, the step right after selection. A title with no
track of the kind never gets there, so what the hook records is what the run kept.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, cast

import pytest

from unshackle.commands.dl import dl
from unshackle.core.api.handlers import DEFAULT_DOWNLOAD_PARAMS
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Series
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video


class Selected(BaseException):
    """Carries the kept track ids out of the loop."""

    def __init__(self, tracks: Tracks) -> None:
        self.ids = {
            "video": [t.id for t in tracks.videos],
            "audio": [t.id for t in tracks.audio],
            "subs": [t.id for t in tracks.subtitles],
        }


class Harness(dl):
    def __getattr__(self, name: str) -> None:
        # every optional dl attribute (tmdb ids, cdm, vaults, ...) reads as unset
        return None


class FakeService:
    """One episode per entry; ``ad`` adds a descriptive track, ``forced`` a forced subtitle."""

    def __init__(self, episodes: dict[int, dict[str, bool]]) -> None:
        self.episodes = episodes
        self.session = None

    def authenticate(self, cookies: Any = None, credential: Any = None) -> None:
        pass

    def get_titles_cached(self) -> Series:
        return Series(
            Episode(id_=f"show-s01-e{n:03}", service=FakeService, title="Show", season=1, number=n, language="en")
            for n in self.episodes
        )

    def get_tracks(self, title: Episode) -> Tracks:
        kinds = self.episodes[title.number]
        tracks = Tracks()
        tracks.add(Video(id_="v", url="https://x/v", language="en", codec=Video.Codec.AVC, range_=Video.Range.SDR))
        tracks.add(Audio(id_="a", url="https://x/a", language="en", codec=Audio.Codec.AAC, bitrate=128_000))
        if kinds.get("ad"):
            tracks.add(
                Audio(id_="ad", url="https://x/ad", language="en", codec=Audio.Codec.AAC, bitrate=1, descriptive=True)
            )
        tracks.add(Subtitle(id_="s", url="https://x/s", language="en", codec=Subtitle.Codec.WebVTT))
        if kinds.get("forced"):
            tracks.add(Subtitle(id_="sf", url="https://x/sf", language="en", codec=Subtitle.Codec.WebVTT, forced=True))
        return tracks

    def get_chapters(self, title: Episode) -> list:
        return []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("on_"):
            # the event hooks (on_track_downloaded, ...) the loop subscribes
            return lambda *a, **k: None
        raise AttributeError(name)


def run(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, episodes: dict, **flags: Any) -> Any:
    """Return the Selected ids, the exit code, or None for a run that completes."""
    monkeypatch.setattr(Tracks, "tree", lambda self, *a, **k: (_ for _ in ()).throw(Selected(self)))
    cmd = Harness.__new__(Harness)
    cmd.log = logging.getLogger("test_kind_only")
    cmd.service = "FAKE"
    cmd.tmdb_searched = True
    cmd.completed_files = []
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(dl.result).parameters.items():
        if name in DEFAULT_DOWNLOAD_PARAMS:
            kwargs[name] = DEFAULT_DOWNLOAD_PARAMS[name]
        elif name not in ("self", "service"):
            kwargs[name] = None if param.default is inspect.Parameter.empty else param.default
    kwargs.update(quality=[], vcodec=[], acodec=[], range_=[], wanted=[], cdm_only=None, downloads=1)
    kwargs.update(flags)
    caplog.set_level(logging.WARNING, logger="test_kind_only")
    try:
        cmd.result(cast(Service, FakeService(episodes)), **kwargs)
    except Selected as sel:
        return sel.ids
    except SystemExit as exc:
        return exc.code
    return None


def test_ado_keeps_the_descriptive_track_and_the_other_types(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, {1: {"ad": True, "forced": True}}, audio_description_only=True)
    assert ids == {"video": ["v"], "audio": ["ad"], "subs": ["s"]}


def test_fso_keeps_the_forced_track_and_the_other_types(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, {1: {"ad": True, "forced": True}}, forced_subs_only=True)
    assert ids == {"video": ["v"], "audio": ["a"], "subs": ["sf"]}


def test_only_flags_with_their_type_flags_give_a_single_kind(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, {1: {"ad": True, "forced": True}}, audio_description_only=True, audio_only=True)
    assert ids == {"video": [], "audio": ["ad"], "subs": []}
    ids = run(monkeypatch, caplog, {1: {"ad": True, "forced": True}}, forced_subs_only=True, subs_only=True)
    assert ids == {"video": [], "audio": [], "subs": ["sf"]}


@pytest.mark.parametrize("flag", ["audio_description_only", "forced_subs_only"])
def test_a_single_title_with_no_such_track_exits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, flag: str
) -> None:
    assert run(monkeypatch, caplog, {1: {}}, **{flag: True}) == 1
    assert "Skipping" not in caplog.text


def test_a_multi_title_run_skips_the_title_and_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = run(monkeypatch, caplog, {1: {}, 2: {"ad": True}}, audio_description_only=True)
    assert ids["audio"] == ["ad"]
    assert "Skipping Show S01E01" in caplog.text


def test_a_multi_title_run_with_every_title_skipped_completes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert run(monkeypatch, caplog, {1: {}, 2: {}}, forced_subs_only=True) is None
    assert caplog.text.count("Skipping Show S01E") == 2
