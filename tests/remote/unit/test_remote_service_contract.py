"""Contract tests for the RemoteService adapter.

RemoteService copies the Service interface but does not inherit from it. When a
method is added to the base Service and wired into dl.py, nothing forces it onto
the adapter, so it can go missing here until a real download calls it. These
tests pin the methods dl.py depends on so a gap fails in CI, not on the devbox.
"""

from __future__ import annotations

import pytest

from unshackle.core.remote_service import RemoteService

pytestmark = pytest.mark.unit

SERVICE_CONTRACT = (
    "authenticate",
    "get_titles",
    "get_titles_cached",
    "get_tracks",
    "get_chapters",
    "get_widevine_license",
    "get_playready_license",
    "get_clearkey_license",
    "get_widevine_service_certificate",
    "resolve_server_keys",
    "on_segment_downloaded",
    "on_track_downloaded",
    "on_track_decrypted",
    "on_track_repacked",
    "on_track_multiplex",
    "close",
)


@pytest.mark.parametrize("name", SERVICE_CONTRACT)
def test_remote_service_implements_contract(name: str) -> None:
    assert callable(getattr(RemoteService, name, None)), f"RemoteService is missing {name}()"


def test_get_clearkey_license_returns_none() -> None:
    # Regression: clearkey uses no CDM, so the adapter returns None and the
    # framework POSTs the manifest Laurl itself. __new__ skips the networked
    # __init__, so the method must not touch self.
    svc = RemoteService.__new__(RemoteService)
    assert svc.get_clearkey_license(challenge=b"", title=None, track=None) is None
