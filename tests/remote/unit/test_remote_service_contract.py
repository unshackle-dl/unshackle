"""Contract tests for the RemoteService adapter.

RemoteService copies the Service interface but does not inherit from it. When a
method is added to the base Service and wired into dl.py, nothing forces it onto
the adapter, so it can go missing here until a real download calls it. These
tests pin the methods dl.py depends on so a gap fails in CI, not on the devbox.
"""

from __future__ import annotations

from collections import deque
from threading import Event
from types import SimpleNamespace
from typing import Any

import click
import pytest

from unshackle.core.remote_service import RemoteService
from unshackle.core.tracks import Video

pytestmark = pytest.mark.unit

SERVICE_CONTRACT = (
    "authenticate",
    "get_titles",
    "get_titles_cached",
    "get_tracks",
    "refresh_track_filters",
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


def test_refresh_track_filters_recreates_the_remote_session(monkeypatch: pytest.MonkeyPatch) -> None:
    parent = click.Context(click.Command("dl"))
    parent.params = {"vcodec": [Video.Codec.AVC], "range_": [Video.Range.SDR]}
    svc = RemoteService.__new__(RemoteService)
    svc.ctx = click.Context(click.Command("REMOTE"), parent=parent)
    svc.credential = SimpleNamespace(username="user")
    svc._keepalive_stop = Event()
    svc._keepalive_stop.set()
    old_stop = svc._keepalive_stop
    svc._log_seq = 10
    svc._recent_logs = deque(["old session"], maxlen=3)
    svc.auth_notice = "old session"
    svc._titles = [object()]
    svc._tracks_by_title = {"title": object()}
    svc._chapters_by_title = {"title": []}
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(svc, "close", lambda: calls.append(("close", None)))
    monkeypatch.setattr(svc, "authenticate", lambda credential=None: calls.append(("authenticate", credential)))
    monkeypatch.setattr(svc, "get_titles", lambda: calls.append(("get_titles", None)))

    svc.refresh_track_filters([Video.Codec.HEVC], [Video.Range.DV], forced_subs=True)

    assert parent.params["vcodec"] == [Video.Codec.HEVC]
    assert parent.params["range_"] == [Video.Range.DV]
    assert parent.params["forced_subs"] is True
    assert svc._tracks_by_title == {}
    assert svc._chapters_by_title == {}
    assert svc._titles is None
    assert svc._log_seq == 0
    assert list(svc._recent_logs) == []
    assert svc.auth_notice is None
    assert svc._keepalive_stop is not old_stop
    assert old_stop.is_set()
    assert not svc._keepalive_stop.is_set()
    assert calls == [("close", None), ("authenticate", svc.credential), ("get_titles", None)]


def test_refresh_track_filters_can_clear_an_unavailable_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    parent = click.Context(click.Command("dl"))
    parent.params = {"vcodec": [Video.Codec.HEVC], "range_": [Video.Range.DV]}
    svc = RemoteService.__new__(RemoteService)
    svc.ctx = click.Context(click.Command("REMOTE"), parent=parent)
    svc.credential = None
    svc._keepalive_stop = Event()
    svc._recent_logs = deque(maxlen=3)
    svc._tracks_by_title = {}
    svc._chapters_by_title = {}
    calls: list[str] = []
    monkeypatch.setattr(svc, "close", lambda: calls.append("close"))
    monkeypatch.setattr(svc, "authenticate", lambda credential=None: calls.append("authenticate"))
    monkeypatch.setattr(svc, "get_titles", lambda: calls.append("get_titles"))

    svc.refresh_track_filters([], [])

    assert parent.params["vcodec"] == []
    assert parent.params["range_"] == []
    assert calls == ["close", "authenticate", "get_titles"]


def test_keepalive_thread_captures_its_own_stop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    class OldStop:
        def wait(self, timeout: float) -> bool:
            del timeout
            return True

    class NewStop:
        def wait(self, timeout: float) -> bool:
            del timeout
            raise AssertionError("old keepalive read the replacement session event")

    target: list[Any] = []

    class FakeThread:
        def __init__(self, *, target: Any, **kwargs: Any) -> None:
            del kwargs
            self.target = target

        def start(self) -> None:
            target.append(self.target)

    svc = RemoteService.__new__(RemoteService)
    svc._session_id = "old"
    svc._keepalive_stop = OldStop()
    svc.service_tag = "TEST"
    svc.client = SimpleNamespace(
        server_url="https://server.example",
        get_optional=lambda endpoint: {"expires_in": 300},
        session=SimpleNamespace(get=lambda *args, **kwargs: pytest.fail("stopped thread must not ping")),
    )
    monkeypatch.setattr("unshackle.core.remote_service.Thread", FakeThread)

    svc.start_keepalive()
    svc._keepalive_stop = NewStop()
    target[0]()
