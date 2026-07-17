"""Regression test for DASH ``org.w3.clearkey`` licensing in ``DASH.download_track``.

The download path gated content-key licensing on ``isinstance(drm, (Widevine,
PlayReady))``, so a ClearKeyCENC track never reached its license callback, left
``content_keys`` empty, and aborted at ``drm.decrypt`` with "Cannot decrypt a
Track without any Content Keys". This drives the real ``download_track`` under
``DOWNLOAD_LICENCE_ONLY`` (returns straight after licensing, no segment fetch or
decrypt) and asserts the ClearKeyCENC drm is licensed.
"""

from __future__ import annotations

from functools import partial
from typing import Any
from uuid import UUID

from unshackle.core.constants import DOWNLOAD_LICENCE_ONLY
from unshackle.core.drm.clearkey_cenc import ClearKeyCENC
from unshackle.core.manifests.dash import DASH
from unshackle.core.tracks.track import DownloadContext

KID = UUID("9eb4050d-e44b-4802-932e-27d75083e266")
KEY = bytes.fromhex("ccd0064c43f7e9fcbaa9b12af3fd1f40")
LAURL = "https://license.example.test/clearkey"
MANIFEST_URL = "https://example.test/manifest.mpd"

MPD = f"""<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2013"
     xmlns:dashif="https://dashif.org/CPS" type="static" mediaPresentationDuration="PT10S">
  <Period id="0">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="video1" codecs="avc1.640028" width="1920" height="1080" bandwidth="5000000">
        <ContentProtection schemeIdUri="urn:uuid:e2719d58-a985-b3c9-781a-b030af78d30e"
                           value="ClearKey1.0" cenc:default_KID="{KID}">
          <dashif:Laurl>{LAURL}</dashif:Laurl>
        </ContentProtection>
        <SegmentList timescale="1000">
          <SegmentURL media="https://example.test/seg1.m4s" duration="1000"/>
        </SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


class RecordingLicence:
    """License callback stub: records its call and populates content keys."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    def __call__(self, drm: Any, track_kid: Any = None) -> None:
        self.calls.append((drm, track_kid))
        for kid in drm.kids:
            drm.content_keys[kid] = KEY.hex()


def test_download_track_licenses_clearkey_cenc(tmp_path, monkeypatch) -> None:
    track = DASH.from_text(MPD, MANIFEST_URL).to_tracks().videos[0]
    # KID normally comes from the init segment; short-circuit the probe (offline).
    monkeypatch.setattr(track, "get_key_id", lambda *args, **kwargs: KID)

    licence = RecordingLicence()
    ctx = DownloadContext(
        save_path=tmp_path / "video.mp4",
        save_dir=tmp_path / "video_segments",
        progress=partial(lambda **_: None),
        license_widevine=licence,
        cdm=None,
    )

    DOWNLOAD_LICENCE_ONLY.set()
    try:
        DASH.download_track(track=track, ctx=ctx)
    finally:
        DOWNLOAD_LICENCE_ONLY.clear()

    assert len(licence.calls) == 1, "ClearKeyCENC track was never licensed"
    drm, track_kid = licence.calls[0]
    assert isinstance(drm, ClearKeyCENC)
    assert track_kid == KID
    # with content keys populated, decrypt has what it needs
    assert drm.content_keys == {KID: KEY.hex()}
