"""Exercise the real selection, publication and completion loop with an in-memory service."""

import inspect
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from unshackle.commands.dl import dl
from unshackle.core.api.handlers import DEFAULT_DOWNLOAD_PARAMS
from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED, DOWNLOAD_LICENCE_ONLY
from unshackle.core.download_archive import DownloadArchive
from unshackle.core.titles import Episode, Series
from unshackle.core.tracks import Audio, Tracks, Video


class EXAMPLE:
    NO_SUBTITLES = True

    def __init__(self, height=720, count=1):
        self.height = height
        self.count = count
        self.session = requests.Session()

    def authenticate(self, *args):
        pass

    def get_titles_cached(self):
        return Series(
            Episode(id_=f"example-episode-{n}", service=EXAMPLE, title="Example", season=1, number=n, language="en")
            for n in range(1, self.count + 1)
        )

    def get_tracks(self, title):
        return Tracks(
            [
                Video(
                    id_=f"v-{title.number}",
                    url="https://example.invalid/video",
                    language="en",
                    codec=Video.Codec.AVC,
                    range_=Video.Range.SDR,
                    height=self.height,
                    width=self.height * 16 // 9,
                    bitrate=1_000_000,
                ),
                Audio(
                    id_=f"a-{title.number}",
                    url="https://example.invalid/audio",
                    language="en",
                    codec=Audio.Codec.AAC,
                    bitrate=128_000,
                ),
            ]
        )

    def get_chapters(self, title):
        return []

    def close(self):
        self.session.close()

    def __getattr__(self, name):
        if name.startswith(("on_", "get_")):
            return lambda *a, **k: None
        raise AttributeError(name)


class Harness(dl):
    def __getattr__(self, name):
        return None


@pytest.fixture
def run(monkeypatch, tmp_path):
    import unshackle.commands.dl as module

    monkeypatch.setattr(config.directories, "cache", tmp_path / "cache")
    monkeypatch.setattr(config.directories, "downloads", tmp_path / "downloads")
    monkeypatch.setattr(config.directories, "temp", tmp_path / "temp")
    monkeypatch.setattr(config, "subtitle", {})
    monkeypatch.setattr(module.providers, "search_metadata", lambda *a, **k: None)
    monkeypatch.setattr(
        module, "load_tracks", lambda service, title, **k: setattr(title, "tracks", service.get_tracks(title))
    )
    monkeypatch.setattr(module.tags, "tag_file", lambda *a, **k: None)
    monkeypatch.setattr(module.MediaInfo, "parse", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(
        Episode,
        "get_filename",
        lambda self, *a, folder=False, **k: "Example" if folder else f"Example.S01E{self.number:02}",
    )
    monkeypatch.setattr(module, "dispatch", lambda *a, **k: None)
    downloads = []
    muxes = []

    def download(tracks, *a, **k):
        for track in tracks:
            config.directories.temp.mkdir(parents=True, exist_ok=True)
            track.path = config.directories.temp / f"{track.id}.bin"
            track.path.write_bytes(b"test")
            downloads.append(track.id)

    monkeypatch.setattr(module, "download_tracks_in_passes", download)

    def mux(self, *a, **k):
        muxes.append(list(self.videos))
        path = config.directories.temp / f"mux-{len(muxes)}.mkv"
        path.write_bytes(b"test mux")
        return path, 0, []

    monkeypatch.setattr(Tracks, "mux", mux)

    def invoke(height=720, count=1, scope="cli", **flags):
        cmd = Harness.__new__(Harness)
        cmd.log = logging.getLogger("archive-test")
        cmd.service = "EXAMPLE"
        cmd.completed_files = []
        cmd.already_present = []
        cmd.skipped_titles = []
        cmd.skipped_subtitles = []
        cmd.archive_scope = scope
        cmd.archive_cache = None
        kwargs = {}
        for name, param in inspect.signature(dl.result).parameters.items():
            if name in DEFAULT_DOWNLOAD_PARAMS:
                kwargs[name] = DEFAULT_DOWNLOAD_PARAMS[name]
            elif name not in ("self", "service"):
                kwargs[name] = None if param.default is inspect.Parameter.empty else param.default
        kwargs.update(
            quality=[], vcodec=[], acodec=[], range_=[], wanted=[], cdm_only=None, downloads=1, skip_existing=True
        )
        kwargs.update(flags)
        try:
            cmd.result(EXAMPLE(height, count), **kwargs)
        finally:
            DOWNLOAD_CANCELLED.clear()
            DOWNLOAD_LICENCE_ONLY.clear()
        return cmd

    invoke.downloads = downloads
    invoke.muxes = muxes
    return invoke


def test_second_run_skips_and_force_downloads(run):
    first = run()
    assert len(first.completed_files) == 1 and not first.already_present
    count = len(run.downloads)
    progress = []
    second = run(progress_sink=progress.append)
    assert len(run.downloads) == count
    assert not second.completed_files and not second.skipped_titles
    assert second.already_present[0]["reason"] == "already_present"
    assert any(event.get("already_present") for event in progress)
    forced = run(force=True)
    assert len(run.downloads) > count
    assert forced.completed_files and not forced.already_present


def test_new_best_quality_downloads_instead_of_skipping(run):
    run(height=720)
    count = len(run.downloads)
    assert run(height=1080).completed_files
    assert len(run.downloads) > count
    assert run(height=1080).already_present


def test_failed_publication_does_not_record(monkeypatch, run):
    import unshackle.commands.dl as module

    monkeypatch.setattr(module.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("publication failed")))
    monkeypatch.setattr(module.shutil, "move", lambda *a: (_ for _ in ()).throw(OSError("publication failed")))
    with pytest.raises(OSError, match="publication failed"):
        run()
    assert not DownloadArchive(config.directories.cache).path.exists()


def test_all_archived_titles_complete_successfully(run):
    run(count=2)
    result = run(count=2)
    assert len(result.already_present) == 2
    assert not result.completed_files and not result.skipped_titles


def test_api_scopes_and_unowned_worker_do_not_use_cli_archive(run):
    run()
    assert run(scope="api-" + "a" * 64).completed_files
    assert run(scope="api-" + "b" * 64).completed_files
    assert run(scope="api-" + "a" * 64).already_present
    assert run(scope="api-unowned").completed_files


def test_mux_failure_does_not_record(monkeypatch, run):
    monkeypatch.setattr(Tracks, "mux", lambda *a, **k: (Path("unused.mkv"), 2, ["mux failed"]))
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 1
    assert not DownloadArchive(config.directories.cache).path.exists()


def test_download_failure_does_not_record(monkeypatch, run):
    import unshackle.commands.dl as module

    def fail(*a, **k):
        raise RuntimeError("test download failure")

    monkeypatch.setattr(module, "download_tracks_in_passes", fail)
    result = run()
    assert result.download_error is not None
    assert not DownloadArchive(config.directories.cache).path.exists()


def test_opt_out_does_not_read_or_write_archive(run):
    run()
    result = run(skip_existing=False)
    assert result.completed_files and not result.already_present


def test_missing_requested_quality_still_fails(run):
    run(height=720)
    with pytest.raises(SystemExit) as exc:
        run(height=720, quality=[1080])
    assert exc.value.code == 1


def test_partial_publication_of_multiple_variants_does_not_record(monkeypatch, run):
    import unshackle.commands.dl as module

    get_tracks = EXAMPLE.get_tracks
    replace_file = module.os.replace

    def variants(self, title):
        tracks = get_tracks(self, title)
        tracks.add(
            Video(
                id_="v-higher",
                url="https://example.invalid/video",
                language="en",
                codec=Video.Codec.AVC,
                range_=Video.Range.SDR,
                height=1080,
                width=1920,
                bitrate=2_000_000,
            )
        )
        return tracks

    monkeypatch.setattr(EXAMPLE, "get_tracks", variants)
    published = []

    def replace(source, target):
        if published:
            raise OSError("second variant failed")
        replace_file(source, target)
        published.append(target)

    def fail_move(*a, **k):
        raise OSError("second variant failed")

    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(module.shutil, "move", fail_move)
    with pytest.raises(OSError, match="second variant failed"):
        run(quality=[720, 1080])
    assert len(published) == 1 and published[0].exists()
    assert not DownloadArchive(config.directories.cache).path.exists()
