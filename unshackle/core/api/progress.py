"""Aggregate job-level download progress for the REST API.

Turns the per-track progress callables from ``Tracks.tree`` into one signal a job can report:
a bitrate-weighted percentage, track counts, and the labels of the tracks downloading now.
"""

from __future__ import annotations

from typing import Any, Callable

from unshackle.core.constants import AnyTrack

JOB_PROGRESS_TERMINAL_STATES = {"Downloaded", "Decrypted", "[yellow]SKIPPED"}

# Weight for a track with no bitrate (subtitles); small vs media bitrates so subs barely move the bar.
SUBTITLE_PROGRESS_WEIGHT = 50_000.0

# Downloads fill 0..this; dl.result drives the remainder (repackaging, muxing) up to 100.
DOWNLOAD_PROGRESS_CEILING = 90.0


def track_progress_label(track: AnyTrack) -> str:
    """Short label for a track, e.g. "video 2160p DV", "audio en-US 5.1", "subtitle fr"."""
    track_type = type(track).__name__
    if track_type == "Video":
        parts = ["video"]
        height = getattr(track, "height", None)
        if height:
            parts.append(f"{height}p")
        track_range = getattr(track, "range", None)
        if track_range is not None:
            parts.append(track_range.value)
        return " ".join(parts)
    if track_type == "Audio":
        parts = ["audio"]
        language = getattr(track, "language", None)
        if language:
            parts.append(str(language))
        channels = getattr(track, "channels", None)
        if channels:
            parts.append(str(channels))
        return " ".join(parts)
    if track_type == "Subtitle":
        language = getattr(track, "language", None)
        return f"subtitle {language}" if language else "subtitle"
    return track_type.lower()


def track_progress_weight(track: AnyTrack) -> float:
    """Track weight in the aggregate (its bitrate in bits/s), so video/audio dominate subtitles."""
    bitrate = getattr(track, "bitrate", None)
    return float(bitrate) if bitrate else SUBTITLE_PROGRESS_WEIGHT


def build_job_progress_callables(
    tracks: list[AnyTrack],
    inner_callables: list[Callable[..., None]],
    sink: Callable[[dict[str, Any]], None],
) -> list[Callable[..., None]]:
    """Wrap each track's progress callable so ``sink`` receives aggregate job progress.

    The sink gets a bitrate-weighted mean completion across all tracks, ``completed_tracks`` /
    ``total_tracks`` counts, and ``active_tracks`` labels. Each track's fraction is monotonic, so
    the percentage only climbs. The original ``inner`` callable is always invoked.
    """
    total = len(inner_callables)
    weights = [track_progress_weight(t) for t in tracks]
    labels = [track_progress_label(t) for t in tracks]
    total_weight = sum(weights) or 1.0
    fractions = [0.0] * total
    done = [False] * total
    started = [False] * total

    def emit() -> None:
        completed = sum(done)
        # Downloads fill 0..DOWNLOAD_PROGRESS_CEILING; dl.result drives muxing up to 100.
        progress = sum(w * f for w, f in zip(weights, fractions)) * DOWNLOAD_PROGRESS_CEILING / total_weight
        active = [labels[i] for i in range(total) if started[i] and not done[i]]
        if active:
            phase = "downloading " + ", ".join(active[:3])
            if len(active) > 3:
                phase += f" (+{len(active) - 3} more)"
        else:
            phase = f"downloading {completed}/{total} tracks"
        sink(
            {
                "progress": progress,
                "phase": phase,
                "completed_tracks": completed,
                "total_tracks": total,
                "active_tracks": active,
                "status": "downloading",
            }
        )

    def wrap(index: int, inner: Callable[..., None]) -> Callable[..., None]:
        counts = {"completed": 0.0, "total": 0.0}

        def tee(*args: Any, **kwargs: Any) -> None:
            started[index] = True
            if kwargs.get("total"):
                counts["total"] = kwargs["total"]
            if kwargs.get("completed") is not None:
                counts["completed"] = kwargs["completed"]
            if "advance" in kwargs:
                counts["completed"] += kwargs["advance"]
            if kwargs.get("downloaded") in JOB_PROGRESS_TERMINAL_STATES:
                done[index] = True
                fractions[index] = 1.0
            elif counts["total"]:
                # max() keeps the fraction monotonic across the download->decrypt callable reuse.
                fractions[index] = max(fractions[index], min(1.0, counts["completed"] / counts["total"]))
            emit()
            return inner(*args, **kwargs)

        return tee

    return [wrap(i, inner) for i, inner in enumerate(inner_callables)]
