"""Tests for ``dl.write_export`` — the ``--export`` JSON sidecar.

Regression: DRM-free tracks never pass through ``prepare_drm``, so ``write_export``
must accept ``drm=None`` (and DRM systems without ``to_dict``/``content_keys`` such
as ClearKey) and still record the track/manifest/chapter/attachment info that
``unshackle import`` rebuilds a download from.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from unshackle.commands.dl import dl
from unshackle.core.drm import drm_from_dict
from unshackle.core.drm.clearkey import ClearKey
from unshackle.core.drm.clearkey_cenc import ClearKeyCENC
from unshackle.core.import_service import ImportService
from unshackle.core.titles import Movie
from unshackle.core.tracks import Audio, Chapter, Subtitle, Video

KID = UUID(hex="00000000000000000000000000000001")


class StubService:
    """Stands in for the service class slot on Movie; never instantiated."""


class StubDRM:
    """Minimal licensed-DRM shape: ``to_dict`` plus filled ``content_keys``."""

    def __init__(self) -> None:
        self.content_keys = {KID: "aa" * 16}

    def to_dict(self) -> dict:
        return {"system": "Widevine", "pssh_b64": "AAAA"}


def make_dl() -> dl:
    # __new__ skips the CLI-driven __init__; write_export only needs `service`.
    instance = dl.__new__(dl)
    instance.service = "EXAMPLE"
    return instance


def make_title() -> Movie:
    title = Movie(id_="movie-1", service=StubService, name="Example Movie", year=2024, language="en")
    title.tracks.add(
        Video(
            id_="v1",
            url="https://example.test/v1.mp4",
            language="en",
            codec=Video.Codec.AVC,
            range_=Video.Range.SDR,
            width=1920,
            height=1080,
            bitrate=5_000_000,
        )
    )
    title.tracks.add(
        Audio(
            id_="a1",
            url="https://example.test/a1.mp4",
            language="en",
            codec=Audio.Codec.AAC,
            bitrate=128_000,
        )
    )
    title.tracks.add(
        Subtitle(
            id_="s1",
            url="https://example.test/s1.vtt",
            language="en",
            codec=Subtitle.Codec.WebVTT,
        )
    )
    return title


def read_export(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf8"))


def test_drm_free_track_exports(tmp_path: Path) -> None:
    """The reported bug: DRM-free downloads produced no usable export."""
    export = tmp_path / "export.json"
    title = make_title()
    video = title.tracks.videos[0]

    make_dl().write_export(export, title, video)

    doc = read_export(export)
    assert doc["version"] == 2
    assert doc["service"] == "EXAMPLE"
    tinfo = doc["titles"]["movie-1"]
    assert tinfo["meta"]["name"] == "Example Movie"
    assert set(tinfo["tracks"]) == {"v1", "a1", "s1"}
    assert "keys" not in tinfo["tracks"]["v1"]
    assert "drm" not in tinfo["tracks"]["v1"]


def test_clearkey_drm_exports_track_without_keys(tmp_path: Path) -> None:
    """ClearKey has no to_dict/content_keys; the track info must still export."""
    export = tmp_path / "export.json"
    title = make_title()
    video = title.tracks.videos[0]

    make_dl().write_export(export, title, video, ClearKey(key="bb" * 16))

    doc = read_export(export)
    track = doc["titles"]["movie-1"]["tracks"]["v1"]
    assert "keys" not in track
    assert "drm" not in track


def test_post_download_write_keeps_licensed_keys(tmp_path: Path) -> None:
    """The drm=None write after download must not clobber prepare_drm's DRM/keys."""
    export = tmp_path / "export.json"
    title = make_title()
    video = title.tracks.videos[0]
    runner = make_dl()

    runner.write_export(export, title, video, StubDRM())  # prepare_drm
    runner.write_export(export, title, video)  # post-download hook

    track = read_export(export)["titles"]["movie-1"]["tracks"]["v1"]
    assert track["drm"] == [{"system": "Widevine", "pssh_b64": "AAAA"}]
    assert track["keys"] == {KID.hex: "aa" * 16}


def test_drm_free_export_roundtrips_through_import_service(tmp_path: Path) -> None:
    """A DRM-free export must rebuild via ImportService: titles, tracks (no DRM),
    empty key pool (resolve_server_keys no-op) and chapters."""
    export = tmp_path / "export.json"
    title = make_title()
    title.tracks.chapters.add(Chapter("00:00:10.000", "Intro"))
    runner = make_dl()
    for track in [*title.tracks.videos, *title.tracks.audio, *title.tracks.subtitles]:
        runner.write_export(export, title, track)

    # ImportService only touches ctx.parent.params (proxy flags) when building its session.
    ctx = SimpleNamespace(parent=None, params={})
    svc = ImportService(ctx, "EXAMPLE", "movie-1", str(export))

    titles = list(svc.get_titles())
    assert len(titles) == 1
    movie = titles[0]
    assert isinstance(movie, Movie)
    assert movie.name == "Example Movie"
    assert movie.year == 2024

    tracks = svc.get_tracks(movie)
    assert {t.id for t in tracks} == {"v1", "a1", "s1"}
    assert all(not t.drm for t in tracks)

    assert svc.key_pool() == {}
    movie.tracks = tracks
    svc.resolve_server_keys(movie)
    assert all(not t.drm for t in movie.tracks)

    # Chapters.add auto-inserts a nameless 00:00:00 baseline chapter; it round-trips too.
    assert [c.name for c in svc.get_chapters(movie)] == [None, "Intro"]


def test_clearkey_cenc_exports_drm_and_keys(tmp_path: Path) -> None:
    """A licensed ClearKeyCENC exports its system dict and KID:KEY map, and the
    exported DRM dict plus keys rebuild a decrypt-ready instance via drm_from_dict."""
    export = tmp_path / "export.json"
    title = make_title()
    video = title.tracks.videos[0]
    drm = ClearKeyCENC(kids=[KID], laurl="https://license.example.test/ck", content_keys={KID: "cc" * 16})

    make_dl().write_export(export, title, video, drm)

    track = read_export(export)["titles"]["movie-1"]["tracks"]["v1"]
    assert track["drm"] == [{"system": "ClearKeyCENC", "kids": [KID.hex], "laurl": "https://license.example.test/ck"}]
    assert track["keys"] == {KID.hex: "cc" * 16}

    rebuilt = drm_from_dict({**track["drm"][0], "content_keys": track["keys"]})
    assert isinstance(rebuilt, ClearKeyCENC)
    assert rebuilt.content_keys == {KID: "cc" * 16}


def test_keyless_content_keys_writes_no_keys_entry(tmp_path: Path) -> None:
    """A DRM object with empty content_keys must not create an empty keys map."""
    export = tmp_path / "export.json"
    title = make_title()
    video = title.tracks.videos[0]
    drm = StubDRM()
    drm.content_keys = {}

    make_dl().write_export(export, title, video, drm)

    track = read_export(export)["titles"]["movie-1"]["tracks"]["v1"]
    assert track["drm"] == [{"system": "Widevine", "pssh_b64": "AAAA"}]
    assert "keys" not in track
