"""The mux step of a Movie or Episode: one muxed file per video group and audio codec, with hybrid DV."""

from __future__ import annotations

import logging
import shutil
import sys
from collections.abc import Callable, Sequence
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any

import click
from pymediainfo import MediaInfo
from rich.padding import Padding
from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn, TimeRemainingColumn

from unshackle.core.config import config
from unshackle.core.console import GradientPulseBarColumn, SyncLive, console
from unshackle.core.placement import Placement
from unshackle.core.titles import Movie
from unshackle.core.titles.episode import Episode
from unshackle.core.tracks import Audio, Tracks, Video
from unshackle.core.tracks.dv_fixup import apply_dv_fixup
from unshackle.core.tracks.hybrid import Hybrid
from unshackle.core.tracks.selection import select_hybrid_bases
from unshackle.core.utilities import standard_resolution

MuxOutput = tuple[Path, Audio.Codec | None]


def group_videos_by_variant(videos: list[Video], *, merge: bool) -> list[list[Video]]:
    """Group video tracks for muxing.

    When ``merge`` is True, this function groups tracks that share ``(height, range, codec)``
    into one file, so only language varies within a group. Different resolutions, ranges and
    codecs stay in separate groups (separate files). When False, each track is its own group
    (one file per track, the default behaviour). Group order follows first-seen track order.
    """
    if not merge:
        return [[video] for video in videos]
    groups: dict[tuple[Any, ...], list[Video]] = {}
    for video in videos:
        groups.setdefault((video.height, video.range, video.codec), []).append(video)
    return list(groups.values())


def hybrid_plan(videos: Sequence[Video], generate_dv: bool) -> tuple[list[Video], Video | None]:
    """The hybrid bases and the 2D DV track to pair with them.

    With no DV track, a base qualifies only with ``--generate-dv``. A resolution with a
    finished hybrid (``native_hybrid``) gets no base: that track is its hybrid.
    """
    dv_tracks = [v for v in videos if v.range == Video.Range.DV and not v.stereo]
    native_heights = {v.height for v in videos if v.native_hybrid}
    base_pool = [v for v in videos if v.height not in native_heights and (dv_tracks or generate_dv)]
    matching_dv = min(dv_tracks, key=lambda v: v.height or 0) if dv_tracks else None
    return select_hybrid_bases(base_pool), matching_dv


def hybrid_source_tracks(base: Video, dv: Video | None, base_container: Path | None = None) -> list[Video]:
    """Copies of the base and DV tracks for the hybrid step, with the base on its container file.

    The hybrid step reads the duration and seeks, and the raw stream from the DV fixup
    permits neither. With no DV track, the hybrid step makes Dolby Vision for the base.
    """
    tracks = [deepcopy(track) for track in (base, dv) if track]
    tracks[0].path = base_container or base.path
    return tracks


def hybrid_step_error(tracks: list[Video], source: str, output_name: str) -> str | None:
    """Run the hybrid step for one resolution; the error that failed it, or None.

    A failure here is this resolution's alone: the title goes on with its other resolutions.
    """
    try:
        Hybrid(tracks, source, output_name=output_name)
    except (ValueError, KeyError, OSError, RuntimeError) as e:
        return str(e) or type(e).__name__
    return None


def missing_hybrid_error(bases: Sequence[Video], made: int) -> str | None:
    """Why the title has no hybrid file, or None when at least one was made."""
    if made:
        return None
    if not bases:
        return "No HEVC HDR10 or HDR10+ track to make a hybrid from"
    return f"Every hybrid failed ({', '.join(f'{v.height or 0}p' for v in bases)}), so the title has no hybrid file"


def mux_title(
    title: Movie | Episode,
    placement: Placement,
    *,
    log: logging.Logger,
    service_tag: str,
    split_audio: bool | None,
    merge_video: bool | None,
    audio_expected: bool,
    skip_subtitles: bool,
    quality: Sequence[Any],
    range_: Sequence[Video.Range],
    vcodec: Sequence[Any],
    generate_dv: bool,
    progress_sink: Callable[[dict[str, Any]], None] | None,
    write_sidecars: Callable[[str, Path], list[Path]] | None,
) -> tuple[list[MuxOutput], dict[Path | None, list[Path]]]:
    """Mux the tracks of a title, then delete them; the muxed files and the sidecar files of each.

    Each output is a muxed file with the audio codec it holds when the audio is split by codec.
    A failed mux exits the run.
    """
    outputs: list[MuxOutput] = []
    sidecars: dict[Path | None, list[Path]] = {}
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        SpinnerColumn(finished_text=""),
        GradientPulseBarColumn(),
        "•",
        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
        "•",
        TextColumn("{task.fields[downloaded]}"),
        console=console,
    )

    merge_audio = (not split_audio) if split_audio is not None else config.muxing.get("merge_audio", True)

    merge_video = merge_video if merge_video is not None else config.muxing.get("merge_video", False)

    multiplex_tasks: list[tuple[TaskID, Tracks, Audio.Codec | None]] = []
    mux_futures: list[futures.Future[tuple[Path, int, list[str]]]] = []
    # Track hybrid-processing outputs explicitly so we can always clean them up,
    # even if muxing fails early (e.g. SystemExit) before the normal delete loop.
    hybrid_temp_paths: list[Path] = []
    hybrid_base_ids: set[str] = set()
    container_paths: dict[str, Path] = {}

    def clone_tracks_for_audio(base_tracks: Tracks, audio_tracks: list[Audio]) -> Tracks:
        task_tracks = Tracks()
        task_tracks.videos = list(base_tracks.videos)
        task_tracks.audio = audio_tracks
        task_tracks.subtitles = list(base_tracks.subtitles)
        task_tracks.chapters = base_tracks.chapters
        task_tracks.attachments = list(base_tracks.attachments)
        return task_tracks

    def unique_mux_output(task_tracks: Tracks, index: int) -> Path | None:
        src: Path | None = None
        ext = ".muxed.mkv"
        if task_tracks.videos:
            src = task_tracks.videos[0].path
        elif task_tracks.audio:
            src, ext = task_tracks.audio[0].path, ".muxed.mka"
        elif task_tracks.subtitles:
            src, ext = task_tracks.subtitles[0].path, ".muxed.mks"
        if src is None:
            return None
        base = src.with_suffix(ext)
        return base.with_name(f"{base.stem}.{index}{base.suffix}")

    def mux_one(mux_index: int, task_id: TaskID, task_tracks: Tracks) -> tuple[Path, int, list[str]]:
        progress.start_task(task_id)
        muxed_path, return_code, errors = task_tracks.mux(
            str(title),
            progress=partial(progress.update, task_id=task_id),
            delete=False,
            audio_expected=audio_expected,
            title_language=title.language,
            skip_subtitles=skip_subtitles,
            output_path=unique_mux_output(task_tracks, mux_index),
        )
        return muxed_path, return_code, errors

    def enqueue_mux_tasks(task_description: str, base_tracks: Tracks, hybrid: bool = False) -> None:
        pool = hybrid_mux_pool if hybrid else mux_pool
        if merge_audio or not base_tracks.audio:
            task_id = progress.add_task(f"{task_description}...", total=None, start=False, downloaded="")
            multiplex_tasks.append((task_id, base_tracks, None))
            mux_futures.append(pool.submit(mux_one, len(multiplex_tasks), task_id, base_tracks))
            return

        audio_by_codec: dict[Audio.Codec | None, list[Audio]] = {}
        for audio_track in base_tracks.audio:
            audio_by_codec.setdefault(audio_track.codec, []).append(audio_track)

        for audio_codec, codec_audio_tracks in audio_by_codec.items():
            description = task_description
            if audio_codec:
                description = f"{task_description} {audio_codec.name}"

            task_id = progress.add_task(f"{description}...", total=None, start=False, downloaded="")
            task_tracks = clone_tracks_for_audio(base_tracks, codec_audio_tracks)
            multiplex_tasks.append((task_id, task_tracks, audio_codec))
            mux_futures.append(pool.submit(mux_one, len(multiplex_tasks), task_id, task_tracks))

    def mux_video_group(video_tracks: list[Video | None]) -> None:
        for video_track in video_tracks:
            if video_track and video_track.dv_compatible_bitstream:
                container = apply_dv_fixup(video_track, keep_source=video_track.id in hybrid_base_ids)
                if container:
                    container_paths[video_track.id] = container
                    hybrid_temp_paths.append(container)

        task_description = "Multiplexing"
        # All tracks in a merged group share height/range/codec, so describe from the first.
        head = next((v for v in video_tracks if v), None)
        if head:
            if len(quality) > 1:
                task_description += f" {standard_resolution(head.width, head.height)}p"
            if len(range_) > 1:
                task_description += f" {head.range.name}"
            if len(vcodec) > 1:
                assert head.codec is not None
                task_description += f" {head.codec.name}"

        task_tracks = Tracks(title.tracks) + title.tracks.chapters + title.tracks.attachments
        if head:
            task_tracks.videos = [v for v in video_tracks if v]

        enqueue_mux_tasks(task_description, task_tracks)

    def group_file_size(video_tracks: Sequence[Video | None]) -> int:
        return sum(v.path.stat().st_size for v in video_tracks if v and v.path and v.path.exists())

    if progress_sink:
        progress_sink({"phase": "muxing", "progress": 96.0, "status": "downloading", "active_tracks": []})

    try:
        with SyncLive(Padding(progress, (0, 5, 1, 5)), console=console, refresh_per_second=20):
            mux_failed = False
            mux_concurrency = max(1, int(config.muxing.get("concurrency", 4)))

            with (
                ThreadPoolExecutor(mux_concurrency) as mux_pool,
                ThreadPoolExecutor(mux_concurrency) as extra_mux_pool,
            ):
                hybrid_mux_pool = mux_pool if mux_concurrency == 1 else extra_mux_pool

                if any(r == Video.Range.HYBRID for r in range_) and title.tracks.videos:
                    log.info("Processing Hybrid HDR10+DV tracks...")

                    original_videos = list(title.tracks.videos)

                    standalone_videos = [v for v in original_videos if not v.hybrid_base_only]
                    standalone_groups: list[list[Video | None]] = [
                        list(g) for g in group_videos_by_variant(standalone_videos, merge=merge_video)
                    ]
                    hybrid_bases, matching_dv = hybrid_plan(
                        [v for v in original_videos if v.hybrid_ingredient], generate_dv
                    )
                    hybrid_base_ids.update(v.id for v in hybrid_bases)
                    hybrids_made = 0
                    for video in (v for v in original_videos if v.native_hybrid):
                        log.info(f"The {video.height}p {video.range_label} track is already the hybrid")
                        hybrids_made += 1

                    for group in sorted(standalone_groups, key=group_file_size, reverse=True):
                        mux_video_group(group)

                    for hdr10_track in hybrid_bases:
                        resolution = hdr10_track.height or 0
                        container = container_paths.get(hdr10_track.id)
                        resolution_tracks = hybrid_source_tracks(hdr10_track, matching_dv, container)

                        hybrid_filename = f"HDR10-DV-{resolution}p.hevc"
                        hybrid_output_path = config.directories.temp / hybrid_filename
                        hybrid_temp_paths.append(hybrid_output_path)

                        step_error = hybrid_step_error(resolution_tracks, service_tag, hybrid_filename)
                        if step_error:
                            # the other files of the title are already queued for muxing
                            log.error(f"Hybrid {resolution}p failed, skipping it: {step_error}")
                            continue
                        hybrids_made += 1

                        if container:
                            try:
                                container.unlink(missing_ok=True)
                            except PermissionError:
                                pass  # still in hybrid_temp_paths, so the final cleanup tries again

                        task_description = f"Multiplexing Hybrid HDR10+DV {resolution}p"
                        task_tracks = Tracks(title.tracks) + title.tracks.chapters + title.tracks.attachments

                        hybrid_track = deepcopy(hdr10_track)
                        hybrid_track.id = f"hybrid_{hdr10_track.id}_{resolution}"
                        hybrid_track.path = hybrid_output_path
                        hybrid_track.range = Video.Range.DV
                        hybrid_track.needs_duration_fix = True
                        title.tracks.add(hybrid_track)
                        task_tracks.videos = [hybrid_track]

                        enqueue_mux_tasks(task_description, task_tracks, hybrid=True)

                    no_hybrid = missing_hybrid_error(hybrid_bases, hybrids_made)
                    if no_hybrid:
                        raise click.ClickException(no_hybrid)

                    console.print()
                else:
                    groups: list[list[Video | None]] = [
                        list(g) for g in group_videos_by_variant(title.tracks.videos, merge=merge_video)
                    ] or [[None]]
                    for group in sorted(groups, key=group_file_size, reverse=True):
                        mux_video_group(group)

            mux_results = [f.result() for f in mux_futures]
            for (_, _, audio_codec), (muxed_path, return_code, errors) in zip(
                multiplex_tasks, mux_results, strict=True
            ):
                outputs.append((muxed_path, audio_codec))
                if return_code >= 2:
                    log.error(f"Failed to Mux video to Matroska file ({return_code}):")
                elif return_code == 1 or errors:
                    log.warning("mkvmerge had at least one warning or error, continuing anyway...")
                for line in errors:
                    if line.startswith("#GUI#error"):
                        log.error(line)
                    else:
                        log.warning(line)
                if return_code >= 2:
                    mux_failed = True
            if mux_failed:
                sys.exit(1)

            if write_sidecars:
                sidecar_targets: list[tuple[Path | None, str, Path]] = []
                for path, _ in outputs:
                    media_info = MediaInfo.parse(path)
                    sidecar_targets.append((path, placement.name(media_info), placement.folder(media_info)))
                if not sidecar_targets:
                    sidecar_targets.append((None, str(title), placement.folder(None)))

                with console.status("Saving subtitle sidecar files..."):
                    first_path, first_base, first_dir = sidecar_targets[0]
                    first_dir.mkdir(parents=True, exist_ok=True)
                    created = write_sidecars(first_base, first_dir)
                    if created:
                        sidecars[first_path] = list(created)
                        log.info(f"Saved {len(created)} sidecar subtitle files")

                    for sidecar_path, base_filename, sidecar_dir in sidecar_targets[1:]:
                        sidecar_dir.mkdir(parents=True, exist_ok=True)
                        copies = []
                        for source in created:
                            target = sidecar_dir / f"{base_filename}{source.name[len(first_base) :]}"
                            if target != source:
                                shutil.copy2(source, target)
                            copies.append(target)
                        if copies:
                            sidecars[sidecar_path] = copies
                            log.info(f"Saved {len(copies)} sidecar subtitle files")

            for track in title.tracks:
                track.delete()

            for attachment in title.tracks.attachments:
                attachment.delete()
    finally:
        # Hybrid() writes a temp HEVC per resolution, so delete each one here.
        for temp_path in hybrid_temp_paths:
            try:
                temp_path.unlink(missing_ok=True)
            except PermissionError:
                log.warning(f"Failed to delete temp file (in use?): {temp_path}")
        for leftover in (
            "HDR10-DV.hevc",
            "HDR10.hevc",
            "DV.hevc",
            "RPU.bin",
            "RPU_UNT.bin",
            "RPU_L5.bin",
            "RPU_L6.bin",
            "L5.json",
            "HDR10Plus.json",
        ):
            leftover_path = config.directories.temp / leftover
            try:
                leftover_path.unlink(missing_ok=True)
            except PermissionError:
                log.warning(f"Failed to delete temp file (in use?): {leftover_path}")

    return outputs, sidecars
