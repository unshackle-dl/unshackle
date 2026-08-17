"""Offline unit tests for the cross-run segment-resume sidecar: fingerprint stability
across rotating query tokens, sensitivity to real segmentation changes, sidecar
read/write robustness, and the sidecar-outside-save_dir invariant that protects the
merge glob from concatenating it into the output."""

from __future__ import annotations

import json
from pathlib import Path

from unshackle.core.tracks import resume

MPD = "https://cdn/manifest.mpd"
SEGMENTS = [
    ("https://cdn/media.mp4", "863-1999"),
    ("https://cdn/media.mp4", "2000-3999"),
    ("https://cdn/media.mp4", "4000-5999"),
]


def test_identical_inputs_identical_digest():
    assert resume.fingerprint(MPD, SEGMENTS) == resume.fingerprint(MPD, SEGMENTS)


def test_rotating_query_token_ignored():
    tokenised = [(f"{url}?token=abc123&expires=999", rng) for url, rng in SEGMENTS]
    assert resume.fingerprint(f"{MPD}?auth=xyz", tokenised) == resume.fingerprint(MPD, SEGMENTS)


def test_changed_byte_range_changes_digest():
    changed = [SEGMENTS[0], ("https://cdn/media.mp4", "2000-4000"), SEGMENTS[2]]
    assert resume.fingerprint(MPD, changed) != resume.fingerprint(MPD, SEGMENTS)


def test_changed_segment_count_changes_digest():
    assert resume.fingerprint(MPD, SEGMENTS[:2]) != resume.fingerprint(MPD, SEGMENTS)


def test_changed_url_path_changes_digest():
    moved = [("https://cdn/other.mp4", rng) for _, rng in SEGMENTS]
    assert resume.fingerprint(MPD, moved) != resume.fingerprint(MPD, SEGMENTS)


def test_query_carried_identity_is_not_collapsed():
    # all segments share a path and differ only in the query (e.g. $Time$ as a query
    # param): stripping must not collapse them into false matches; the digest goes
    # verbatim and distinct segmentations stay distinct
    a = [("https://cdn/seg?t=0", None), ("https://cdn/seg?t=100", None)]
    b = [("https://cdn/seg?t=0", None), ("https://cdn/seg?t=200", None)]
    assert resume.fingerprint(MPD, a) != resume.fingerprint(MPD, b)


def test_extra_inputs_change_digest():
    assert resume.fingerprint(MPD, SEGMENTS, extra=["100", "3"]) != resume.fingerprint(MPD, SEGMENTS)


def test_reusable_roundtrip(tmp_path: Path):
    save_dir = tmp_path / "Video_x_segments"
    digest = resume.fingerprint(MPD, SEGMENTS)
    resume.write_sidecar(save_dir, digest)
    assert resume.reusable(save_dir, digest) is True
    assert resume.reusable(save_dir, "0" * 64) is False


def test_reusable_never_raises(tmp_path: Path):
    save_dir = tmp_path / "Video_x_segments"
    digest = resume.fingerprint(MPD, SEGMENTS)
    assert resume.reusable(save_dir, digest) is False  # no sidecar
    resume.sidecar_path(save_dir).write_text("{not json", encoding="utf-8")
    assert resume.reusable(save_dir, digest) is False  # malformed
    resume.sidecar_path(save_dir).write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert resume.reusable(save_dir, digest) is False  # no digest key


def test_clear_sidecar(tmp_path: Path):
    save_dir = tmp_path / "Video_x_segments"
    digest = resume.fingerprint(MPD, SEGMENTS)
    resume.write_sidecar(save_dir, digest)
    resume.clear_sidecar(save_dir)
    assert resume.reusable(save_dir, digest) is False
    resume.clear_sidecar(save_dir)  # idempotent on a missing file


def test_sidecar_is_never_inside_save_dir(tmp_path: Path):
    save_dir = tmp_path / "Video_x_segments"
    assert resume.sidecar_path(save_dir).parent == save_dir.parent
