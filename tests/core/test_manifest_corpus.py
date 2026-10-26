"""Parse every public test manifest in tests/fixtures/manifests (refresh: scripts/fetch_manifest_corpus.py)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Union
from unittest.mock import MagicMock

import pytest

from unshackle.core.manifests import DASH, HLS, ISM
from unshackle.core.tracks import Audio, Tracks, Video
from unshackle.core.utils.xml import load_xml

CORPUS = Path(__file__).resolve().parent.parent / "fixtures" / "manifests"
INDEX: list[dict[str, Any]] = json.loads((CORPUS / "index.json").read_text(encoding="utf-8"))

# file -> reason; strict, so a fix that makes one parse fails here until its entry is removed
KNOWN_FAILURES: dict[str, str] = {}


def cases(entries: list[dict[str, Any]]) -> list[Any]:
    return [
        pytest.param(e, id=e["file"], marks=pytest.mark.xfail(reason=KNOWN_FAILURES[e["file"]], strict=True))
        if e["file"] in KNOWN_FAILURES
        else pytest.param(e, id=e["file"])
        for e in entries
    ]


def parse(entry: dict[str, Any]) -> Tracks:
    """Parse a fixture the way from_url does: XML from bytes, M3U8 decoded as UTF-8."""
    data = (CORPUS / entry["file"]).read_bytes()
    parser: Union[DASH, HLS, ISM]
    if entry["kind"] == "hls":
        parser = HLS.from_text(data.decode("utf-8"), entry["url"])
    else:
        parser = (DASH if entry["kind"] == "dash" else ISM)(load_xml(data), entry["url"])
    return parser.to_tracks("en")


def most_per_set(tracks: list[Any]) -> int:
    return max(Counter(id(t.data["dash"]["adaptation_set"]) for t in tracks).values(), default=0)


class NeedsNetwork(Exception):
    pass


def test_index_lists_every_known_failure() -> None:
    assert INDEX
    assert set(KNOWN_FAILURES) <= {e["file"] for e in INDEX}


@pytest.mark.parametrize("entry", cases(INDEX))
def test_manifest_parses_to_tracks(entry: dict[str, Any]) -> None:
    tracks = parse(entry)

    assert tracks.videos or tracks.audio or tracks.subtitles
    for track in tracks:
        # HLS CODECS is only a SHOULD (RFC 8216 4.3.4.2), so an HLS track can honestly have none
        assert track.codec is not None or entry["kind"] == "hls", track
        assert track.language, track

    expect = entry.get("expect")
    if expect:
        assert most_per_set(tracks.videos) == expect["max_num_video"]
        assert most_per_set(tracks.audio) == expect["max_num_audio"]
        for codec_type, codecs, found in (
            (Video.Codec, expect["video_codec"], tracks.videos),
            (Audio.Codec, expect["audio_codec"], tracks.audio),
        ):
            if codecs:
                assert {codec_type.from_codecs(c) for c in codecs.split(",")} <= {t.codec for t in found}


@pytest.mark.parametrize(
    "entry",
    [
        e
        for e in INDEX
        if e["kind"] == "dash"
        and e["file"] not in KNOWN_FAILURES
        and b'type="dynamic"' not in (CORPUS / e["file"]).read_bytes()
    ],
    ids=lambda e: e["file"],
)
def test_dash_segments_and_drm_resolve_offline(entry: dict[str, Any]) -> None:
    """Build the DRM and the segment list of every track; a track whose segment index must be fetched is skipped."""
    session = MagicMock()
    session.get.side_effect = NeedsNetwork
    for track in parse(entry):
        dash = track.data["dash"]
        manifest, adaptation_set, representation = dash["manifest"], dash["adaptation_set"], dash["representation"]
        DASH.get_drm(representation.findall("ContentProtection") + adaptation_set.findall("ContentProtection"))
        period = dash["period"] if dash.get("period") is not None else manifest.find("Period")
        try:
            _, segments, _, _, _ = DASH.get_period_segments(
                period, adaptation_set, representation, manifest, track, track.url, session, probe_kid=False
            )
        except NeedsNetwork:
            continue
        assert segments, track
        assert all(url.startswith(("http://", "https://")) for url, _ in segments), track
