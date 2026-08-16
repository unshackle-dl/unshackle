"""ProtectionHeader SystemID parsing.

MS-SSTR manifests commonly write the GUID in registry format
("{EDEF8BA9-...}"); a bare-GUID comparison silently drops the DRM.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pywidevine import PSSH

from unshackle.core.drm import Widevine
from unshackle.core.manifests.ism import ISM

WIDEVINE_PSSH_B64 = PSSH.new(key_ids=["00112233445566778899aabbccddeeff"], system_id=PSSH.SystemId.Widevine).dumps()

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<SmoothStreamingMedia MajorVersion="2" MinorVersion="2" Duration="600000000" TimeScale="10000000">
  <Protection>
    <ProtectionHeader SystemID="{system_id}">{pssh}</ProtectionHeader>
  </Protection>
  <StreamIndex Type="video" QualityLevels="1" Chunks="1" Url="QualityLevels({{bitrate}})/Fragments(video={{start time}})">
    <QualityLevel Index="0" Bitrate="1000000" FourCC="H264" MaxWidth="1280" MaxHeight="720"
      CodecPrivateData="00000001674d401e9a6602800b76020000003e90000bb800f18311200000000168ebccb22c"/>
    <c t="0" d="20000000"/>
  </StreamIndex>
</SmoothStreamingMedia>
"""


@pytest.mark.parametrize(
    "system_id",
    [
        "EDEF8BA9-79D6-4ACE-A3C8-27DCD51D21ED",
        "{EDEF8BA9-79D6-4ACE-A3C8-27DCD51D21ED}",
    ],
)
def test_get_drm_resolves_braced_and_bare_system_ids(system_id):
    manifest = MANIFEST.format(system_id=system_id, pssh=WIDEVINE_PSSH_B64)
    ism = ISM.from_text(manifest, "https://example.test/Manifest")
    drm = ISM.get_drm(ism.manifest.xpath(".//ProtectionHeader"))
    assert len(drm) == 1
    assert isinstance(drm[0], Widevine)
    assert drm[0].kid == UUID("00112233-4455-6677-8899-aabbccddeeff")


def test_get_drm_warns_on_unparseable_header(caplog):
    manifest = MANIFEST.format(system_id="EDEF8BA9-79D6-4ACE-A3C8-27DCD51D21ED", pssh="!!!not-base64!!!")
    ism = ISM.from_text(manifest, "https://example.test/Manifest")
    with caplog.at_level("WARNING", logger="ISM"):
        drm = ISM.get_drm(ism.manifest.xpath(".//ProtectionHeader"))
    assert drm == []
    assert any("unparseable Widevine" in r.message for r in caplog.records)
