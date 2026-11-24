"""The segment pipeline on its own: no manifest, no HTTP server, a fake downloader that writes the segment files."""

from __future__ import annotations

from concurrent.futures import Future
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from uuid import UUID

import pytest

from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.drm import verify
from unshackle.core.events import events
from unshackle.core.manifests.segment_pipeline import (
    RollingMerge,
    check_merged,
    complete_track,
    fetch_segments,
    license_drm,
    licensing,
)
from unshackle.core.tracks.track import DownloadContext

INIT = b"init" * 8
DECRYPTED_INIT = b"clear-init" * 8
PARTS = [bytes([0xA0 + i]) * (40 + 7 * i) for i in range(4)]
ARRIVAL = [2, 0, 3, 1]
KID = UUID(hex="11111111111111111111111111111111")
SESSION = SimpleNamespace(headers={"User-Agent": "EXAMPLE"}, cookies={})


@pytest.fixture(autouse=True)
def no_cancel() -> Iterator[None]:
    DOWNLOAD_CANCELLED.clear()
    yield
    DOWNLOAD_CANCELLED.clear()


@pytest.fixture
def seen() -> Iterator[list[tuple[str, Any]]]:
    """Every pipeline event as (name, detail); a segment event records whether its file still existed."""
    log: list[tuple[str, Any]] = []
    handlers = {
        events.Types.SEGMENT_DOWNLOADED: lambda track, segment: log.append(("segment", segment.exists())),
        events.Types.TRACK_DOWNLOADED: lambda track: log.append(("downloaded", track.path)),
        events.Types.TRACK_DECRYPTED: lambda track, drm, segment: log.append(("decrypted", drm)),
    }
    for kind, handler in handlers.items():
        events.subscribe(kind, handler)
    yield log
    for kind, handler in handlers.items():
        events.unsubscribe(kind, handler)


def make_ctx(tmp_path: Path, **kw: Any) -> DownloadContext:
    statuses: list[str] = []

    def progress(**update: Any) -> None:
        if update.get("downloaded"):
            statuses.append(update["downloaded"])

    ctx = DownloadContext(
        save_path=tmp_path / "track.mp4",
        save_dir=tmp_path / "track.mp4_segments",
        progress=partial(progress),
        proxy="http://proxy.example:8080",
        max_workers=3,
        **kw,
    )
    ctx.statuses = statuses  # type: ignore[attr-defined]
    return ctx


class FakeDownloader:
    """Writes PARTS in ARRIVAL order and reports each file, with one speed update; can fail after some segments."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.fail_after = fail_after
        self.kwargs: dict[str, Any] = {}
        self.closed = False

    def __call__(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        self.kwargs = kwargs
        try:
            yield {"downloaded": "1.0 MB/s"}
            for n, index in enumerate(ARRIVAL):
                if n == self.fail_after:
                    raise ConnectionError("EXAMPLE segment failed")
                path = kwargs["output_dir"] / f"{index}.mp4"
                path.write_bytes(PARTS[index])
                yield {"file_downloaded": path}
        finally:
            self.closed = True


class FakeDecrypter:
    """Hands each segment back as it is, and records the order of its calls."""

    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self.calls = calls

    def init_bytes(self) -> bytes:
        return DECRYPTED_INIT

    def submit(self, segment: Path) -> Future:
        self.calls.append(("submit", segment.exists()))
        future: Future = Future()
        future.set_result(None)
        return future

    def finish(self) -> bytes:
        self.calls.append(("finish", None))
        return DECRYPTED_INIT

    def close(self) -> None:
        self.calls.append(("close", None))


class FakeDRM:
    """decrypt_track calls decrypt() when no hook is installed; it marks the file and records the events seen so far."""

    def __init__(self, seen: list[tuple[str, Any]]) -> None:
        self.seen = seen
        self.events_before: list[str] = []

    def decrypt(self, path: Path) -> None:
        self.events_before = [name for name, _ in self.seen]
        path.write_bytes(b"clear:" + path.read_bytes())


def fetch(ctx: DownloadContext, downloader: FakeDownloader, **kw: Any) -> tuple[RollingMerge | None, bytes | None]:
    ctx.save_dir.mkdir(parents=True, exist_ok=True)
    track = SimpleNamespace(id="EXAMPLE", downloader=downloader)
    urls = [{"url": f"https://cdn.example/{i}.m4s"} for i in range(len(PARTS))]
    return fetch_segments(track, ctx, SESSION, "EXAMPLE", urls, ctx.save_dir, "{i}.mp4", **kw)  # type: ignore[arg-type]


def test_rolling_merge_writes_the_init_then_the_segments_in_order(tmp_path, seen):
    ctx = make_ctx(tmp_path)
    downloader = FakeDownloader()
    merger, head_init = fetch(ctx, downloader, rolling=True, init=INIT)

    assert merger is not None and merger.merged == len(PARTS) and head_init == INIT
    assert ctx.save_path.read_bytes() == INIT + b"".join(PARTS)
    assert not list(ctx.save_dir.iterdir())
    # listeners get each segment while it still exists; the merge unlinks it afterwards
    assert seen == [("segment", True)] * len(PARTS)
    assert ctx.statuses == ["EXAMPLE 1.0 MB/s"]
    assert downloader.closed
    assert downloader.kwargs["proxy"] == ctx.proxy and downloader.kwargs["max_workers"] == ctx.max_workers
    assert downloader.kwargs["headers"] is SESSION.headers and downloader.kwargs["filename"] == "{i}.mp4"


def test_without_rolling_merge_the_segments_stay_for_the_manifest_merge(tmp_path, seen):
    ctx = make_ctx(tmp_path)
    merger, _ = fetch(ctx, FakeDownloader())

    assert merger is None
    assert not ctx.save_path.exists()
    assert sorted(p.name for p in ctx.save_dir.iterdir()) == [f"{i}.mp4" for i in range(len(PARTS))]
    assert len(seen) == len(PARTS)


def test_a_decrypter_gets_each_segment_before_its_event_and_heads_the_output(tmp_path, seen):
    ctx = make_ctx(tmp_path)
    merger, decrypted_init = fetch(ctx, FakeDownloader(), rolling=True, init=INIT, decrypter=FakeDecrypter(seen))

    assert decrypted_init == DECRYPTED_INIT
    assert ctx.save_path.read_bytes() == DECRYPTED_INIT + b"".join(PARTS)
    assert seen == [("submit", True), ("segment", True)] * len(PARTS) + [("finish", None)]


def test_a_failure_closes_the_decrypter_and_removes_the_output(tmp_path, seen):
    ctx = make_ctx(tmp_path)
    calls: list[tuple[str, Any]] = []
    downloader = FakeDownloader(fail_after=2)
    with pytest.raises(ConnectionError, match="EXAMPLE segment failed"):
        fetch(ctx, downloader, rolling=True, init=INIT, decrypter=FakeDecrypter(calls))

    assert downloader.closed
    assert calls[-1] == ("close", None) and ("finish", None) not in calls
    assert not ctx.save_path.exists()


def test_a_short_rolling_merge_raises_and_removes_the_output(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.save_path.write_bytes(INIT)
    merger = RollingMerge(open(ctx.save_path, "ab"))
    merger.dst.close()

    with pytest.raises(FileNotFoundError, match=f"Rolling merge appended 0 of {len(PARTS)} segments"):
        check_merged(merger, len(PARTS), SimpleNamespace(id="EXAMPLE"), ctx, "EXAMPLE")  # type: ignore[arg-type]
    assert not ctx.save_path.exists()
    check_merged(merger, 0, SimpleNamespace(id="EXAMPLE"), ctx, "EXAMPLE")  # type: ignore[arg-type]


def test_license_drm_wraps_the_callback_in_licensing_progress(tmp_path):
    received: list[tuple[Any, dict[str, Any]]] = []
    ctx = make_ctx(tmp_path, license_widevine=lambda drm, **kw: received.append((drm, kw)))
    drm = object()

    with licensing(ctx):
        license_drm(drm, ctx, track_kid=KID)  # type: ignore[arg-type]

    assert received == [(drm, {"track_kid": KID})]
    assert ctx.statuses == ["LICENSING", "[yellow]LICENSED"]
    assert not DOWNLOAD_CANCELLED.is_set()


def test_a_licence_failure_cancels_the_other_tracks_and_shows_failed(tmp_path):
    def refuse(drm: Any, **kw: Any) -> None:
        raise PermissionError("EXAMPLE licence refused")

    ctx = make_ctx(tmp_path, license_widevine=refuse)
    with pytest.raises(PermissionError, match="EXAMPLE licence refused"):
        with licensing(ctx):
            license_drm(object(), ctx)  # type: ignore[arg-type]

    assert ctx.statuses == ["LICENSING", "[red]FAILED"]
    assert DOWNLOAD_CANCELLED.is_set()


def test_license_drm_without_a_callback_raises(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ValueError, match="license_widevine func must be supplied"):
        license_drm(object(), ctx)  # type: ignore[arg-type]
    assert ctx.statuses == []


@pytest.fixture
def no_hook(monkeypatch):
    monkeypatch.setattr(verify, "DECRYPT_HOOK", None)


def test_complete_track_emits_downloaded_then_decrypts_then_cleans_up(tmp_path, seen, no_hook):
    ctx = make_ctx(tmp_path)
    ctx.save_dir.mkdir()
    ctx.save_path.write_bytes(b"merged")
    drm = FakeDRM(seen)
    track = SimpleNamespace(path=None, drm=[drm])

    complete_track(track, ctx, drm)  # type: ignore[arg-type]

    assert drm.events_before == ["downloaded"]
    assert seen == [("downloaded", ctx.save_path), ("decrypted", drm)]
    assert ctx.save_path.read_bytes() == b"clear:merged"
    assert track.path == ctx.save_path and track.drm is None
    assert ctx.statuses == ["Decrypting", "Decrypted", "Downloaded"]
    assert not ctx.save_dir.exists()


def test_complete_track_after_segment_decryption_only_checks(tmp_path, seen, no_hook):
    ctx = make_ctx(tmp_path)
    ctx.save_path.write_bytes(b"merged")
    drm = FakeDRM(seen)

    complete_track(SimpleNamespace(path=None, drm=[drm]), ctx, drm, decrypt=False)  # type: ignore[arg-type]

    assert ctx.save_path.read_bytes() == b"merged"
    assert [name for name, _ in seen] == ["downloaded", "decrypted"]


def test_complete_track_without_drm_skips_the_decryption(tmp_path, seen):
    ctx = make_ctx(tmp_path)
    ctx.save_path.write_bytes(b"merged")

    complete_track(SimpleNamespace(path=None, drm=None), ctx, None)  # type: ignore[arg-type]

    assert seen == [("downloaded", ctx.save_path)]
    assert ctx.statuses == ["Downloaded"]
