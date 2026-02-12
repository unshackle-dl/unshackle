"""Utility functions for parsing M3U8 playlists."""

from __future__ import annotations

from typing import Optional

import m3u8
from requests import Session

from unshackle.core.clients.base import BaseHttpClient
from unshackle.core.manifests.hls import HLS
from unshackle.core.tracks import Tracks


def parse(
    master: m3u8.M3U8,
    language: str,
    *,
    session: Optional[BaseHttpClient] = None,
) -> Tracks:
    """Parse a variant playlist to ``Tracks`` with basic information, defer DRM loading."""
    tracks = HLS(master, session=session).to_tracks(language)

    bool(master.session_keys or HLS.parse_session_data_keys(master, session or Session()))

    if True:
        for t in tracks.videos + tracks.audio:
            t.needs_drm_loading = True
            t.session = session

    return tracks


__all__ = ["parse"]
