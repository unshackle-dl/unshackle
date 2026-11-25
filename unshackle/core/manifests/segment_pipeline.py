"""The segment pipeline: the steps DASH, HLS and ISM share to download a segmented track.

Each manifest finds its own segments and supplies its own quirks; the pipeline runs the
licence step, the download with its rolling merge, the decrypt hand-off and the cleanup.
"""

from __future__ import annotations

import html
import shutil
from concurrent.futures import Future
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, Optional
from uuid import UUID

from unshackle.core.constants import DOWNLOAD_CANCELLED, AnyTrack
from unshackle.core.drm import DRM_T, ClearKeyCENC, MonaLisa, PlayReady, Widevine
from unshackle.core.drm.segment_decrypt import SegmentDecrypter
from unshackle.core.drm.verify import decrypt_track
from unshackle.core.events import events
from unshackle.core.remote_protocol import APIErrorCode, coded
from unshackle.core.session import AnySession
from unshackle.core.tracks import DownloadContext, Track, resume
from unshackle.core.tracks.track import assert_fragments_decrypted
from unshackle.core.utilities import log_event, try_ensure_utf8


def append_segment(dst: BinaryIO, segment_file: Path, is_text_subtitle: bool) -> None:
    """Append one segment file to the output, with the text subtitle fixups when they apply."""
    if is_text_subtitle:
        segment_data = try_ensure_utf8(segment_file.read_bytes())
        segment_data = (
            segment_data.decode("utf8")
            .replace("&lrm;", html.unescape("&lrm;"))
            .replace("&rlm;", html.unescape("&rlm;"))
            .encode("utf8")
        )
        dst.write(segment_data)
    else:
        with open(segment_file, "rb") as src:
            shutil.copyfileobj(src, dst, 1024 * 1024)


class RollingMerge:
    """Appends segments to the output in index order as they arrive, out of order, and unlinks them.

    A cursor holds the next expected index; every `add` records one ready segment and then drains
    every contiguous ready segment from the cursor onward. A segment that carries a future (per-segment
    decryption) waits for that future before the append, so the output never holds raw bytes.

    `append(dst, segment_file)` writes one segment; the default is a plain byte copy. A parser
    whose merge needs a per-segment transform, or a head that only exists once segment 0 has
    landed (ISM), passes its own.
    """

    def __init__(self, dst: BinaryIO, append: Optional[Callable[[BinaryIO, Path], None]] = None) -> None:
        self.dst = dst
        self.append = append or (lambda dst, path: append_segment(dst, path, is_text_subtitle=False))
        self.cursor = 0
        self.ready: dict[int, tuple[Path, Optional[Future]]] = {}

    def add(self, index: int, segment_file: Path, future: Optional[Future] = None) -> None:
        # runs on the downloader's own loop, so a decrypt wait or a catch-up burst
        # delays new submissions; move the drain to a writer thread if throughput measurably drops
        self.ready[index] = (segment_file, future)
        while self.cursor in self.ready:
            path, pending = self.ready.pop(self.cursor)
            if pending is not None:
                pending.result()
            self.append(self.dst, path)
            self.cursor += 1
            # the bytes are already in the output; a handle another process still holds on
            # the just-replaced file (Windows) must not fail the track, the segment dir sweep
            # after the download removes any leftover
            try:
                path.unlink()
            except OSError:
                pass

    @property
    def merged(self) -> int:
        return self.cursor


@contextmanager
def licensing(ctx: DownloadContext) -> Iterator[None]:
    """Show FAILED and cancel the pending track downloads when the body raises."""
    try:
        yield
    except Exception:
        DOWNLOAD_CANCELLED.set()
        ctx.progress(downloaded="[red]FAILED")
        raise


LICENSABLE: dict[Track.Descriptor, tuple[type, ...]] = {
    Track.Descriptor.URL: (Widevine, PlayReady, ClearKeyCENC),
    Track.Descriptor.DASH: (Widevine, PlayReady, ClearKeyCENC),
    Track.Descriptor.HLS: (Widevine, PlayReady, MonaLisa),
    Track.Descriptor.ISM: (Widevine, PlayReady),
}
"""The DRM systems the licence step licenses, by the kind of manifest the track comes from."""


def license_drm(
    track: AnyTrack,
    drm: Optional[DRM_T],
    ctx: DownloadContext,
    kid: Optional[UUID] = None,
    probe: Optional[Callable[[], Optional[UUID]]] = None,
) -> None:
    """The licence step: get the content keys of `drm` for `track` through ctx.license_widevine, with progress.

    A DRM system that LICENSABLE does not list for the track gets no licence. The track KID is `kid`, else
    what `probe` reads from the init. An HLS or ISM DRM comes from the track's own key tag or protection
    header, so its first KID stands in when the init names none. A MonaLisa DRM gets no KID: its ticket gives
    the track's own key. A failed `probe` fails only this track; a failed licence cancels the run.
    ctx.license_widevine licenses every DRM system; dl and services pass it by keyword.
    """
    if not isinstance(drm, LICENSABLE[track.descriptor]):
        return
    track_kid = None
    if not isinstance(drm, MonaLisa):
        track_kid = kid or (probe() if probe else None)
        if not track_kid and track.descriptor in (Track.Descriptor.HLS, Track.Descriptor.ISM):
            track_kid = getattr(drm, "kid", None)
    with licensing(ctx):
        if not ctx.license_widevine:
            raise coded(
                ValueError(f"No licence callback for the {type(drm).__name__} DRM of track {track.id}"),
                APIErrorCode.DRM_ERROR,
            )
        ctx.progress(downloaded="LICENSING")
        ctx.license_widevine(drm, track_kid=track_kid)
        ctx.progress(downloaded="[yellow]LICENSED")


def fetch_segments(
    track: AnyTrack,
    ctx: DownloadContext,
    session: AnySession,
    label: str,
    urls: list[dict[str, Any]],
    output_dir: Path,
    filename: str,
    *,
    rolling: bool = False,
    init: Optional[bytes] = None,
    append: Optional[Callable[[BinaryIO, Path], None]] = None,
    decrypter: Optional[SegmentDecrypter] = None,
) -> tuple[Optional[RollingMerge], Optional[bytes]]:
    """Download the segment requests and return the rolling merge, if any, and the init that heads the output.

    With `rolling`, the init (or the decrypter's decrypted init) and each segment go into ctx.save_path
    as they land. A decrypter gets every segment before its SEGMENT_DOWNLOADED event. Any failure closes
    the decrypter and removes a rolling output. The returned init is the decrypter's decrypted init, else `init`.
    """
    merger: Optional[RollingMerge] = None
    try:
        with ExitStack() as stack:
            if rolling:
                output = stack.enter_context(open(ctx.save_path, "wb"))
                head = decrypter.init_bytes() if decrypter else init
                if head:
                    output.write(head)
                merger = RollingMerge(output, append)

            # an exception raised in this body (a decrypt failure re-raised by the merge)
            # must close the generator now, so its finally aborts the workers
            stream = stack.enter_context(
                closing(
                    track.downloader(
                        urls=urls,
                        output_dir=output_dir,
                        filename=filename,
                        headers=session.headers,
                        cookies=session.cookies,
                        proxy=ctx.proxy,
                        max_workers=ctx.max_workers,
                        session=session,
                        adaptive=ctx.adaptive_workers,
                        processes=ctx.download_processes,
                    )
                )
            )
            for status_update in stream:
                file_downloaded = status_update.get("file_downloaded")
                if file_downloaded:
                    future = decrypter.submit(file_downloaded) if decrypter else None
                    # listeners get the segment while it still exists; the merge unlinks it
                    events.emit(events.Types.SEGMENT_DOWNLOADED, track=track, segment=file_downloaded)
                    if merger:
                        merger.add(int(file_downloaded.stem), file_downloaded, future)
                else:
                    downloaded = status_update.get("downloaded")
                    if downloaded and downloaded.endswith("/s"):
                        status_update["downloaded"] = f"{label} {downloaded}"
                    ctx.progress(**status_update)

        head_init = decrypter.finish() if decrypter else init
    except BaseException:
        if decrypter:
            decrypter.close()
        if merger:
            ctx.save_path.unlink(missing_ok=True)
        raise
    return merger, head_init


def check_merged(merger: RollingMerge, total: int, track: AnyTrack, ctx: DownloadContext, label: str) -> None:
    """Remove the output and raise FileNotFoundError when the rolling merge appended fewer than total segments."""
    if merger.merged == total:
        return
    error_msg = f"Rolling merge appended {merger.merged} of {total} segments: {ctx.save_dir}"
    log_event(
        f"manifest_{label.lower()}_rolling_merge_incomplete",
        level="ERROR",
        message=error_msg,
        context={
            "track_id": getattr(track, "id", None),
            "track_type": track.__class__.__name__,
            "save_dir": str(ctx.save_dir),
            "merged": merger.merged,
            "total_segments": total,
            "downloader": "requests",
        },
    )
    ctx.save_path.unlink(missing_ok=True)
    raise FileNotFoundError(error_msg)


def decrypt_output(track: AnyTrack, ctx: DownloadContext, drm: DRM_T, decrypt: bool = True) -> None:
    """Decrypt ctx.save_path through decrypt_track, check every fragment, and emit TRACK_DECRYPTED.

    `decrypt=False` means a SegmentDecrypter already decrypted the segments, so only the check runs.
    """
    ctx.progress(downloaded="Decrypting", completed=0, total=None)
    decrypt_track(drm, ctx.save_path, ctx.license_widevine, decrypt=decrypt, track=track)
    assert_fragments_decrypted(ctx.save_path)
    track.drm = None
    events.emit(events.Types.TRACK_DECRYPTED, track=track, drm=drm, segment=None)


def remove_segment_dir(save_dir: Path) -> None:
    """Remove the segment directory, also when a superseded hedge download still dropped a .!dev file in it."""
    if save_dir.exists() and save_dir.name.endswith("_segments"):
        try:
            save_dir.rmdir()
        except OSError:
            shutil.rmtree(save_dir, ignore_errors=True)


def complete_track(track: AnyTrack, ctx: DownloadContext, drm: Optional[DRM_T], decrypt: bool = True) -> None:
    """Hand the merged file to the track: TRACK_DOWNLOADED, then the decryption, then the segment directory cleanup."""
    track.path = ctx.save_path
    events.emit(events.Types.TRACK_DOWNLOADED, track=track)

    if drm:
        decrypt_output(track, ctx, drm, decrypt)
        ctx.progress(downloaded="Decrypted", completed=100, total=100)

    remove_segment_dir(ctx.save_dir)
    resume.clear_sidecar(ctx.save_dir)
    ctx.progress(downloaded="Downloaded")


__all__ = (
    "RollingMerge",
    "append_segment",
    "check_merged",
    "complete_track",
    "decrypt_output",
    "fetch_segments",
    "license_drm",
    "licensing",
    "remove_segment_dir",
)
