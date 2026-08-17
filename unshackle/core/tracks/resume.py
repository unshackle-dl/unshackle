"""Cross-run segment-resume sidecars.

A track's completed segment files may be reused by a later run, but only once
the manifest proves the segmentation is unchanged. The proof is a fingerprint
of the segmentation stored in a JSON sidecar next to the segment directory.

The sidecar is deliberately a sibling of the segment directory, never a file
inside it: the manifest parsers merge by globbing the directory with no
extension filter, so any file left in there would be concatenated into the
final output.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

_VERSION = 1


def sidecar_path(save_dir: Path) -> Path:
    """Path of the resume sidecar for save_dir (always a sibling, see module docstring)."""
    return save_dir.with_name(save_dir.name + ".resume.json")


def _strip_url(url: Optional[str]) -> str:
    """Drop query and fragment: auth tokens rotate between runs and would make every digest unique."""
    if not url:
        return ""
    scheme, netloc, path, _, _ = urlsplit(url)
    return urlunsplit((scheme, netloc, path, "", ""))


def fingerprint(
    manifest_url: Optional[str],
    segments: Sequence[tuple[str, Optional[str]]],
    extra: Sequence[str] = (),
) -> str:
    """Stable digest of a track's segmentation.

    Hashes the manifest URL (sans query), the segment count, and each
    segment's URL (sans query) with its byte-range string verbatim; byte
    ranges are the strongest identity signal available and never carry
    tokens. extra carries protocol-specific identity inputs (HLS: the
    media_sequence start and post-filter segment count).
    """
    stripped = [[_strip_url(url), byte_range or ""] for url, byte_range in segments]
    if len({tuple(pair) for pair in stripped}) != len(stripped):
        stripped = [[url, byte_range or ""] for url, byte_range in segments]
    payload = {
        "version": _VERSION,
        "manifest": _strip_url(manifest_url),
        "count": len(segments),
        "segments": stripped,
        "extra": list(extra),
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reusable(save_dir: Path, digest: str) -> bool:
    """True when save_dir's sidecar records this exact digest.

    Never raises: a missing, unreadable, or malformed sidecar means "cannot
    prove reuse is safe", which means restart clean.
    """
    try:
        data = json.loads(sidecar_path(save_dir).read_text(encoding="utf-8"))
        return bool(data.get("digest")) and data["digest"] == digest
    except (OSError, ValueError):
        return False


def write_sidecar(save_dir: Path, digest: str) -> None:
    """Record digest so a later run can prove save_dir's segments are reusable."""
    sidecar_path(save_dir).write_text(json.dumps({"version": _VERSION, "digest": digest}), encoding="utf-8")


def clear_sidecar(save_dir: Path) -> None:
    """Withdraw the reuse proof (merge is about to mutate files, or the track completed)."""
    sidecar_path(save_dir).unlink(missing_ok=True)
