"""Find a track's DRM headers without a licence: from its manifest data, its playlist or its init segment."""

from __future__ import annotations

import logging
from typing import Any

from unshackle.core.drm.playready import PlayReady
from unshackle.core.drm.widevine import Widevine
from unshackle.core.manifests import DASH, ISM
from unshackle.core.tracks import Track

log = logging.getLogger(__name__)


def track_pssh(track: Any, drm_type: str, drm: Any = None) -> str | None:
    """The base64 PSSH of one DRM system's header: ``drm``'s own when it is of that system, else the track's first.

    None when no header of that system has a PSSH.
    """
    for drm_obj in [drm, *(getattr(track, "drm", None) or [])]:
        drm_class = drm_obj.__class__.__name__
        if drm_type == "widevine" and drm_class == "Widevine":
            return str(drm_obj.pssh.dumps())
        if drm_type == "playready" and drm_class == "PlayReady" and drm_obj.pssh_b64:
            return str(drm_obj.pssh_b64)
    return None


def fetch_init_segment(track: Any, session: Any = None) -> bytes | None:
    """The init segment of a DASH track, or the head of a one-file track with DRM; None otherwise, or when the fetch fails."""
    session = getattr(track, "session", None) or session
    if session is None:
        return None

    dash = (getattr(track, "data", None) or {}).get("dash") or {}
    if not all(dash.get(k) is not None for k in ("period", "adaptation_set", "representation", "manifest")):
        if getattr(track, "descriptor", None) is not Track.Descriptor.URL or not getattr(track, "drm", None):
            return None
        try:
            return track.get_init_segment(session=session)
        except Exception as e:
            log.debug(f"Init segment fetch failed for {track.id}: {e!r}")
            return None

    try:
        return DASH.get_period_segments(
            period=dash["period"],
            adaptation_set=dash["adaptation_set"],
            representation=dash["representation"],
            manifest=dash["manifest"],
            track=track,
            track_url=track.url,
            session=session,
        )[0]
    except Exception as e:
        log.warning(f"Init segment fetch failed for {track.id}: {e!r}")
        return None


def drm_from_init_segment(track: Any, session: Any = None, init_data: bytes | None = None) -> list:
    """Read the DRM headers out of a DASH track's init segment.

    Some manifests carry no ContentProtection PSSH; the real header reaches the player only
    in the init segment.
    """
    if init_data is None:
        init_data = fetch_init_segment(track, session)
    if not init_data:
        return []

    drms = []
    for drm_cls in (Widevine, PlayReady):
        try:
            drms.append(drm_cls.from_init_data(init_data))
        except Exception as e:
            log.debug(f"No usable {drm_cls.__name__} PSSH in the init segment of {track.id}: {e!r}")
    return drms


def ensure_track_drm(track: Any, session: Any = None, init_data: bytes | None = None) -> None:
    """Fill ``track.drm`` from the manifest when the track has none; a failure leaves it empty and never raises.

    Reads DASH ContentProtection and then the init segment, the HLS playlist keys, or the ISM ProtectionHeader.
    """
    if track.drm:
        return

    if track.data.get("dash"):
        rep = track.data["dash"].get("representation")
        ada = track.data["dash"].get("adaptation_set")
        if rep is not None and ada is not None:
            track.drm = DASH.get_drm(rep.findall("ContentProtection") + ada.findall("ContentProtection"))
            if track.drm:
                return

        track.drm = drm_from_init_segment(track, session, init_data) or None
        if track.drm:
            return

    if (track.data.get("hls") or getattr(track, "descriptor", None) is Track.Descriptor.HLS) and track.url:
        try:
            if track.load_drm_from_playlist(session):
                return
        except Exception as e:
            log.debug(f"HLS DRM prefetch failed for {track.id}: {e!r}")

    if track.data.get("ism"):
        try:
            manifest = track.data["ism"].get("manifest")
            if manifest is not None:
                track.drm = ISM.get_drm(manifest.xpath(".//ProtectionHeader"))
        # a swallowed error here means the track silently gets no DRM, so it must be loud
        except Exception as e:
            log.warning(f"ISM DRM extraction failed for {track.id}: {e!r}")
