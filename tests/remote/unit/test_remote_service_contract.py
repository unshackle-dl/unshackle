"""RemoteService licence callbacks that must not touch the remote session."""

from __future__ import annotations

import pytest

from unshackle.core.remote_service import RemoteService

pytestmark = pytest.mark.unit


def test_get_clearkey_license_returns_none() -> None:
    # Regression: clearkey uses no CDM, so the adapter returns None and the
    # framework POSTs the manifest Laurl itself. __new__ skips the networked
    # __init__, so the method must not touch self.
    svc = RemoteService.__new__(RemoteService)
    svc._track_alias = {}
    svc._title_alias = {}
    assert svc.get_clearkey_license(challenge=b"", title=None, track=None) is None
