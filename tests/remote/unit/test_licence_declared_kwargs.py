"""The REST API licence paths must give a service only the arguments it declares.

`serve` reaches a service's licence function through three call sites that bypass the
`dl` command: server_cdm Widevine, server_cdm PlayReady, and the proxy-license forward.
Each one now filters with `declared_kwargs`, so a service that never asked for `pssh` or
`session_id` keeps working while a session-aware service finally receives `session_id`
on the server_cdm path.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any, Optional
from uuid import UUID

import pyplayready.system.pssh as pr_pssh_mod
import pytest
import pywidevine.pssh as wv_pssh_mod

import unshackle.core.cdm as cdm_mod
import unshackle.core.cdm.detect as detect_mod
import unshackle.core.drm as drm_mod
from unshackle.core.api import handlers
from unshackle.core.drm import PlayReady, Widevine
from unshackle.core.service import Service

pytestmark = pytest.mark.unit

KID = UUID("11111111111111111111111111111111")
KEY = bytes.fromhex("22222222222222222222222222222222")
PSSH_B64 = base64.b64encode(b"pssh").decode()
SESSION_ID = b"sid-bytes"


class _Recorder:
    """Base for the fake services; every licence call lands in `calls`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_widevine_service_certificate(self, *, challenge: Any, title: Any, track: Any) -> None:
        return None


class ClassicService(_Recorder):
    """EXAMPLE: the pre-`session_id` signature every older service uses."""

    def get_widevine_license(self, *, challenge: bytes, title: Any, track: Any) -> bytes:
        self.calls.append({"challenge": challenge, "title": title, "track": track})
        return b"LIC"

    def get_playready_license(self, *, challenge: bytes, title: Any, track: Any) -> str:
        self.calls.append({"challenge": challenge, "title": title, "track": track})
        return "<License>ok</License>"


class SessionService(_Recorder):
    """DEMO: a session-aware service, the shape that needs the open CDM session id."""

    def get_widevine_license(
        self, *, challenge: bytes, title: Any, track: Any, session_id: Optional[bytes] = None
    ) -> bytes:
        self.calls.append({"challenge": challenge, "title": title, "track": track, "session_id": session_id})
        return b"LIC"

    def get_playready_license(
        self, *, challenge: bytes, title: Any, track: Any, session_id: Optional[bytes] = None
    ) -> str:
        self.calls.append({"challenge": challenge, "title": title, "track": track, "session_id": session_id})
        return "<License>ok</License>"


class VarKwService(_Recorder):
    """DEMO2: a `**kwargs` signature takes whatever the framework offers."""

    def get_widevine_license(self, *, challenge: bytes, **kwargs: Any) -> bytes:
        self.calls.append({"challenge": challenge, **kwargs})
        return b"LIC"

    def get_playready_license(self, *, challenge: bytes, **kwargs: Any) -> str:
        self.calls.append({"challenge": challenge, **kwargs})
        return "<License>ok</License>"


class StubWidevine(Widevine):
    """Real `Widevine.get_content_keys`, with the PSSH parsing skipped."""

    # pssh and kids are read-only properties on the real class; shadow them so a stub can set them
    pssh: Any = None
    kids: Any = None

    def __init__(self, pssh: Any = None, **kwargs: Any) -> None:
        self.pssh = pssh
        self.kids = [KID]
        self.content_keys: dict[UUID, str] = {}


class StubPlayReady(PlayReady):
    """Real `PlayReady.get_content_keys`, with the WRMHEADER parsing skipped."""

    # pssh, pssh_b64 and kids are read-only properties on the real class
    pssh: Any = None
    pssh_b64: Any = None
    kids: Any = None

    def __init__(self, pssh: Any = None, pssh_b64: Optional[str] = None, **kwargs: Any) -> None:
        self.pssh = SimpleNamespace(wrm_headers=["<WRMHEADER/>"])
        self.pssh_b64 = pssh_b64
        self.kids = [KID]
        self.content_keys: dict[UUID, str] = {}

    def extract_keys_from_cdm(self, cdm: Any, session_id: Any) -> dict[UUID, str]:
        return {KID: KEY.hex()}


class FakeWidevineCdm:
    service_certificate_challenge = b"cert-chal"

    def open(self) -> bytes:
        return SESSION_ID

    def get_license_challenge(self, session_id: bytes, pssh: Any) -> bytes:
        return b"CHAL"

    def parse_license(self, session_id: bytes, license_res: Any) -> None:
        return None

    def get_keys(self, session_id: bytes, type_: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(kid=KID, key=KEY)]

    def close(self, session_id: bytes) -> None:
        return None


class FakePlayReadyCdm:
    def open(self) -> bytes:
        return SESSION_ID

    def get_license_challenge(self, session_id: bytes, wrm_header: Any) -> str:
        return "CHAL"

    def parse_license(self, session_id: bytes, license_str: str) -> None:
        return None

    def close(self, session_id: bytes) -> None:
        return None


@pytest.fixture
def widevine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wv_pssh_mod, "PSSH", lambda b64: SimpleNamespace(b64=b64))
    monkeypatch.setattr(drm_mod, "Widevine", StubWidevine)
    monkeypatch.setitem(cdm_mod.__dict__, "load_cdm", lambda *a, **k: FakeWidevineCdm())
    monkeypatch.setattr(detect_mod, "is_widevine_cdm", lambda cdm: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "check_vaults", lambda kids, name: None)
    monkeypatch.setattr(handlers, "cache_to_vaults", lambda keys, name: None)
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)


@pytest.fixture
def playready_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_pssh_mod, "PSSH", lambda data: SimpleNamespace(data=data))
    monkeypatch.setattr(drm_mod, "PlayReady", StubPlayReady)
    monkeypatch.setitem(cdm_mod.__dict__, "load_cdm", lambda *a, **k: FakePlayReadyCdm())
    monkeypatch.setattr(detect_mod, "is_playready_cdm", lambda cdm: True)
    monkeypatch.setattr(handlers, "ensure_track_drm", lambda track, session=None, init_data=None: None)
    monkeypatch.setattr(handlers, "resolve_device_name", lambda *a, **k: "dev")
    monkeypatch.setattr(handlers, "cache_to_vaults", lambda keys, name: None)
    monkeypatch.setattr(handlers.config, "serve", {"users": {}}, raising=False)


def _server_cdm(service: Any, drm_type: str) -> dict[str, str]:
    return handlers.handle_single_server_cdm(
        service=service,
        title=SimpleNamespace(),
        track=SimpleNamespace(),
        pssh_b64=PSSH_B64,
        drm_type=drm_type,
        request=None,
    )


def test_server_cdm_widevine_classic_signature_gets_three_arguments(widevine_env: None) -> None:
    service = ClassicService()
    keys = _server_cdm(service, "widevine")

    assert keys == {KID.hex: KEY.hex()}
    assert len(service.calls) == 1
    assert set(service.calls[0]) == {"challenge", "title", "track"}
    assert service.calls[0]["challenge"] == b"CHAL"


def test_server_cdm_widevine_session_service_receives_the_open_session_id(widevine_env: None) -> None:
    service = SessionService()
    _server_cdm(service, "widevine")

    assert set(service.calls[0]) == {"challenge", "title", "track", "session_id"}
    assert service.calls[0]["session_id"] == SESSION_ID


def test_server_cdm_widevine_var_kwargs_service_receives_pssh_and_session_id(widevine_env: None) -> None:
    service = VarKwService()
    _server_cdm(service, "widevine")

    assert set(service.calls[0]) == {"challenge", "title", "track", "pssh", "session_id"}
    assert service.calls[0]["session_id"] == SESSION_ID


def test_server_cdm_widevine_unnarrowable_signature_retries_without_the_extras(widevine_env: None) -> None:
    # declared_kwargs cannot narrow a signature it cannot read (a compiled service) so it offers
    # everything. The call raises TypeError and Widevine retries with `challenge` alone, which
    # the lambda turns back into the classic challenge/title/track set the service does accept.
    service = ClassicService()
    calls: list[dict[str, Any]] = []

    def compiled(**kw: Any) -> bytes:
        calls.append(kw)
        if {"pssh", "session_id"} & set(kw):
            raise TypeError("unexpected keyword argument")
        return b"LIC"

    service.get_widevine_license = compiled  # type: ignore[method-assign]
    _server_cdm(service, "widevine")

    assert len(calls) == 2
    assert set(calls[0]) == {"challenge", "title", "track", "pssh", "session_id"}
    assert set(calls[1]) == {"challenge", "title", "track"}


def test_server_cdm_widevine_lambda_is_never_itself_filtered(widevine_env: None) -> None:
    # The `(**kw)` lambda would look like a VAR_KEYWORD service if declared_kwargs were ever
    # applied to it, and a classic service would then be handed `pssh` and `session_id`.
    service = ClassicService()
    _server_cdm(service, "widevine")

    assert "pssh" not in service.calls[0]
    assert "session_id" not in service.calls[0]


def test_server_cdm_playready_classic_signature_gets_three_arguments(playready_env: None) -> None:
    service = ClassicService()
    keys = _server_cdm(service, "playready")

    assert keys == {KID.hex: KEY.hex()}
    assert set(service.calls[0]) == {"challenge", "title", "track"}


def test_server_cdm_playready_offers_pssh_b64_not_pssh_or_session_id(playready_env: None) -> None:
    # PlayReady.get_content_keys calls licence(challenge=..., pssh_b64=...); it has no
    # session_id to give, so a session-aware service sees the None default there.
    service = VarKwService()
    _server_cdm(service, "playready")

    assert set(service.calls[0]) == {"challenge", "title", "track", "pssh_b64"}
    assert service.calls[0]["pssh_b64"] == PSSH_B64


def test_server_cdm_playready_session_service_is_not_passed_session_id(playready_env: None) -> None:
    service = SessionService()
    _server_cdm(service, "playready")

    assert set(service.calls[0]) == {"challenge", "title", "track", "session_id"}
    assert service.calls[0]["session_id"] is None  # the signature default, never an offered value


def _proxy(service: Any, drm_type: str) -> Any:
    return handlers.handle_proxy_license(
        service=service,
        title=SimpleNamespace(),
        track=SimpleNamespace(),
        challenge_b64=base64.b64encode(b"CHAL").decode(),
        drm_type=drm_type,
    )


@pytest.mark.parametrize("drm_type", ["widevine", "playready"])
def test_proxy_license_classic_signature(drm_type: str) -> None:
    service = ClassicService()
    response = _proxy(service, drm_type)

    assert response.status == 200
    assert set(service.calls[0]) == {"challenge", "title", "track"}
    assert service.calls[0]["challenge"] == b"CHAL"


@pytest.mark.parametrize("drm_type", ["widevine", "playready"])
def test_proxy_license_var_kwargs_service_gets_no_session_id(drm_type: str) -> None:
    # The client holds the CDM, so there is no server-side session to name. A `**kwargs`
    # service must not see a session_id key at all.
    service = VarKwService()
    _proxy(service, drm_type)

    assert set(service.calls[0]) == {"challenge", "title", "track"}


@pytest.mark.parametrize("drm_type", ["widevine", "playready"])
def test_proxy_license_session_service_sees_its_own_default(drm_type: str) -> None:
    # A session-aware service is called without session_id, so its signature default applies.
    # A service that then uses session_id unconditionally fails here, before and after the
    # declared_kwargs change alike.
    service = SessionService()
    _proxy(service, drm_type)

    assert set(service.calls[0]) == {"challenge", "title", "track", "session_id"}
    assert service.calls[0]["session_id"] is None


def test_proxy_license_rejects_an_unknown_drm_type() -> None:
    from unshackle.core.api.errors import APIError

    with pytest.raises(APIError):
        _proxy(ClassicService(), "monalisa")


class _DelegatingService(Service):
    """A service with no PlayReady implementation: the base class delegates to Widevine."""

    def __init__(self) -> None:  # deliberately skips Service.__init__
        self.calls: list[dict[str, Any]] = []

    def get_titles(self) -> Any: ...  # pragma: no cover - abstract stub

    def get_tracks(self, title: Any) -> Any: ...  # pragma: no cover - abstract stub

    def get_chapters(self, title: Any) -> Any: ...  # pragma: no cover - abstract stub

    def get_widevine_license(self, *, challenge: bytes, title: Any, track: Any) -> bytes:
        self.calls.append({"challenge": challenge, "title": title, "track": track})
        return b"LIC"


class _DelegatingVarKwService(_DelegatingService):
    def get_widevine_license(self, *, challenge: bytes, **kwargs: Any) -> bytes:  # type: ignore[override]
        self.calls.append({"challenge": challenge, **kwargs})
        return b"LIC"


def test_base_playready_delegate_filters_to_the_widevine_signature() -> None:
    service = _DelegatingService()
    assert service.get_playready_license(challenge=b"C", title="T", track="TR") == b"LIC"
    assert set(service.calls[0]) == {"challenge", "title", "track"}


def test_base_playready_delegate_passes_everything_to_a_var_kwargs_service() -> None:
    service = _DelegatingVarKwService()
    service.get_playready_license(challenge=b"C", title="T", track="TR")
    assert set(service.calls[0]) == {"challenge", "title", "track"}


def test_base_playready_delegate_cannot_forward_session_id() -> None:
    # Service.get_playready_license declares (challenge, title, track) only, so the server_cdm
    # filter drops session_id before the delegate ever runs. A session-aware service that wants
    # PlayReady must implement get_playready_license itself.
    from unshackle.core.utilities import declared_kwargs

    offered = {"challenge": b"C", "title": "T", "track": "TR", "session_id": SESSION_ID}
    assert "session_id" not in declared_kwargs(Service.get_playready_license, offered)
