"""Opt-in completion receipts, not an inventory of files currently on disk."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from filelock import FileLock

from unshackle.core.tracks import Video
from unshackle.core.utilities import standard_resolution

PARTIAL_MODES = (
    "sample",
    "skip_dl",
    "no_mux",
    "video_only",
    "audio_only",
    "subs_only",
    "chapters_only",
    "no_video",
    "no_audio",
    "no_subs",
    "no_chapters",
    "no_attachments",
    "audio_description_only",
    "forced_subs_only",
    "cdm_only",
    "list_",
    "list_titles",
    "skip_subtitle_errors",
    "generate_dv",
)


@dataclass(frozen=True)
class ArchiveEntry:
    service: str
    title_id: str | int
    quality: int
    range: str
    codec: str

    @classmethod
    def from_video(cls, service: str, title_id: Any, video: Video) -> Optional[ArchiveEntry]:
        """Use the resolved video, never the requested quality or a filename."""
        quality = standard_resolution(video.width, video.height)
        if (
            type(title_id) not in (str, int)
            or not title_id
            or not quality
            or not isinstance(video.codec, Video.Codec)
            or not isinstance(video.range, Video.Range)
            or video.range == Video.Range.HYBRID
            or video.hybrid_ingredient
            or video.finished_hybrid
            or video.stereo
        ):
            return None
        return cls(service, title_id, quality, video.range.value, video.codec.value)

    def token(self) -> str:
        # JSON preserves integer versus string IDs, unlike str(title.id).
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def archive_entries(service: str, title: Any, options: dict[str, Any]) -> list[ArchiveEntry]:
    """Only fully known ordinary movie/episode variants may use completion receipts."""
    from unshackle.core.titles import Episode, Movie

    if not isinstance(title, (Episode, Movie)) or any(options.get(mode) for mode in PARTIAL_MODES):
        return []
    entries = [ArchiveEntry.from_video(service, title.id, video) for video in title.tracks.videos]
    if not entries or any(entry is None for entry in entries):
        return []
    return list(dict.fromkeys(entry for entry in entries if entry is not None))


class DownloadArchive:
    def __init__(self, cache: Path, scope: str = "cli") -> None:
        # Scope is selected by the CLI or worker envelope, never by request parameters.
        if scope != "cli" and not re.fullmatch(r"api-[0-9a-f]{64}", scope):
            raise ValueError("Invalid download archive scope")
        self.path = cache / "download-archive" / f"{scope}.jsonl"
        self.lock = FileLock(str(self.path) + ".lock", timeout=30)
        self._snapshot: Optional[tuple[int, int, int]] = None
        self._records: set[str] = set()

    def _read(self) -> set[str]:
        if not self.path.exists():
            return set()
        stat = self.path.stat()
        snapshot = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if snapshot == self._snapshot:
            return self._records
        records = set()
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or row.pop("version", None) != 1:
                        continue
                    entry = ArchiveEntry(**row)
                    if (
                        type(entry.title_id) not in (str, int)
                        or not entry.title_id
                        or not isinstance(entry.service, str)
                        or type(entry.quality) is not int
                        or entry.quality <= 0
                        or entry.range not in {r.value for r in Video.Range if r != Video.Range.HYBRID}
                        or entry.codec not in {c.value for c in Video.Codec}
                    ):
                        continue
                    records.add(entry.token())
                except (ValueError, TypeError):
                    # Interrupted appends and unknown schemas are never completion receipts.
                    continue
        self._snapshot = snapshot
        self._records = records
        return records

    def contains(self, entries: Iterable[ArchiveEntry]) -> bool:
        requested = {entry.token() for entry in entries}
        if not requested or not self.path.exists():
            return False
        with self.lock:
            return requested <= self._read()

    def record(self, entries: Iterable[ArchiveEntry]) -> None:
        entries = list(entries)
        if not entries:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize local file I/O only, never a download.
        with self.lock:
            recorded = self._read()
            pending = [entry for entry in entries if entry.token() not in recorded]
            if not pending:
                return
            with self.path.open("a", encoding="utf-8") as handle:
                # Separate a new receipt from a truncated previous append.
                handle.write("\n")
                for entry in pending:
                    handle.write(json.dumps({"version": 1, **asdict(entry)}, ensure_ascii=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                stat = os.fstat(handle.fileno())
            self._records = recorded | {entry.token() for entry in pending}
            self._snapshot = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
