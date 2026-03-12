from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from filelock import FileLock

if TYPE_CHECKING:
    from unshackle.core.titles import Title_T


log = logging.getLogger("SavedTitlesStore")


@dataclass
class SavedTitleRecord:
    title_id: str
    display_name: str

    @classmethod
    def from_line(cls, line: str) -> SavedTitleRecord:
        title_id, separator, display_name = line.rstrip().partition(" | ")
        if not separator:
            raise ValueError(f"Invalid saved title line: {line!r}")
        return cls(title_id.strip(), display_name.strip())

    def merge(self, other: SavedTitleRecord) -> None:
        if other.display_name:
            self.display_name = other.display_name

    def to_line(self) -> str:
        return f"{self.title_id} | {self.display_name}"


class SavedTitlesStore:
    def __init__(self, directory: Path, enabled: bool = True):
        self.directory = Path(directory)
        self.enabled = enabled

    def observe(self, service_name: str, title: Title_T, title_id: Any = None) -> None:
        if not self.enabled:
            return

        record_id = str(title_id if title_id is not None else title.id).strip()
        if not record_id:
            return

        record = SavedTitleRecord(record_id, get_display_name(title))

        path = self.directory / f"{service_name}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(f"{path.suffix}.lock")
        with FileLock(str(lock_path)):
            records = load_records(path)
            current = records.get(record.title_id)
            if current:
                current.merge(record)
            else:
                records[record.title_id] = record

            write_records(path, records)


def get_display_name(title: Any) -> str:
    if hasattr(title, "title") and hasattr(title, "season"):
        name = str(title.title).strip()
        if getattr(title, "year", None):
            name += f" ({title.year})"
        return name

    if hasattr(title, "artist") and hasattr(title, "album"):
        name = f"{title.artist} - {title.album}".strip(" -")
        if getattr(title, "year", None):
            name += f" ({title.year})"
        return name

    if hasattr(title, "name"):
        name = str(title.name).strip()
        if getattr(title, "year", None):
            name += f" ({title.year})"
        return name

    return str(title).strip()


def load_records(path: Path) -> dict[str, SavedTitleRecord]:
    records: dict[str, SavedTitleRecord] = {}
    if not path.exists():
        return records

    for line in path.read_text(encoding="utf8").splitlines():
        if not line.strip():
            continue

        try:
            record = SavedTitleRecord.from_line(line)
        except ValueError as exc:
            log.warning("Skipping invalid saved title line in %s: %s", path, exc)
            continue

        current = records.get(record.title_id)
        if current:
            current.merge(record)
        else:
            records[record.title_id] = record

    return records


def write_records(path: Path, records: dict[str, SavedTitleRecord]) -> None:
    content = "\n".join(item.to_line() for item in records.values()) + "\n"
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(content, encoding="utf8")
    temp_path.replace(path)


@lru_cache(maxsize=1)
def get_saved_titles_store() -> SavedTitlesStore:
    from unshackle.core.config import config

    return SavedTitlesStore(config.directories.saved_titles, config.saved_titles_enabled)


__all__ = ("SavedTitleRecord", "SavedTitlesStore", "get_saved_titles_store")
