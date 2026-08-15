from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def write_music_manifest(path: Path, *, release: dict[str, Any], tracks: list[dict[str, Any]]) -> Path:
    """Write a compact JSON manifest for music integrity, checksums, and metadata results."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "unshackle.music.manifest.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release": release,
        "tracks": tracks,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


__all__ = ("write_music_manifest",)
