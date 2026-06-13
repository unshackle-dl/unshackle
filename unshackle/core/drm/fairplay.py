from __future__ import annotations

import re
from typing import Optional
from uuid import UUID

from pyplayready.system.pssh import PSSH

from unshackle.core.drm.playready import PlayReady, build_pr_header_from_kid


def fairplay_kid_from_skd(skd_uri: str) -> Optional[UUID]:
    """Extract the content KID from a FairPlay ``skd://`` key URI.

    Handles the common multi-DRM packager forms where the KID appears in the URI as
    a hyphenated GUID (optionally followed by ``:<iv>`` or as a ``?KID=<guid>`` query)
    or as a bare 32-hex string in a path segment. Services whose skd URI encodes the
    KID differently (e.g. an asset number plus content id) should derive it themselves
    and use ``FairPlay.from_kid``.
    """
    # Prefer a hyphenated GUID; fall back to a bare 32-hex run.
    guid = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", skd_uri)
    if guid:
        return UUID(hex=guid.group(0))
    match = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])", skd_uri)
    if not match:
        return None
    return UUID(hex=match.group(1))


class FairPlay(PlayReady):
    """FairPlay track licensed through a PlayReady CDM (FairPlay -> PlayReady bridge).

    FairPlay HLS (SAMPLE-AES / cbcs) carries a content KID but no PlayReady PSSH.
    We synthesize a PlayReady header from that KID, so a PlayReady CDM can issue a
    challenge and the resulting license decrypts the cbcs stream. This subclasses
    PlayReady so the CDM challenge and decrypt logic are reused unchanged; the only
    difference is that the download pipeline routes its license request to the
    service's ``get_fairplay_license`` instead of ``get_playready_license``.
    """

    @classmethod
    def from_kid(cls, kid: UUID, scheme: str = "cbcs", version: str = "4.3") -> FairPlay:
        """Build a FairPlay DRM from a bare content KID via a synthesized PlayReady header."""
        pssh_b64 = build_pr_header_from_kid(kid, scheme=scheme, version=version)
        return cls(pssh=PSSH(pssh_b64), kid=kid, pssh_b64=pssh_b64)
