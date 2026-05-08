from __future__ import annotations

import hashlib
from pathlib import Path


def file_md5(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the MD5 checksum for a local file."""
    digest = hashlib.md5()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ("file_md5",)
