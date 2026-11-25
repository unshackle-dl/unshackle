"""Each core raiser names its own API error code, so a reworded message keeps the code."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import click
import pytest
import requests

from unshackle.core import binaries
from unshackle.core import service as service_mod
from unshackle.core.api.errors import categorize_exception
from unshackle.core.api.input_bridge import InputBridge, NoUserBridge
from unshackle.core.drm import ClearKeyCENC, MonaLisa, PlayReady, Widevine, decrypters
from unshackle.core.import_service import ImportService, manifest_fetch_error
from unshackle.core.proxies.gluetun import Gluetun
from unshackle.core.proxies.nordvpn import NordVPN
from unshackle.core.proxies.resolve import Unavailable, find_provider, pick_proxy
from unshackle.core.proxies.surfsharkvpn import SurfsharkVPN
from unshackle.core.remote_protocol import APIErrorCode
from unshackle.core.service import Service
from unshackle.core.utils import ip_info, run_proxy


def code_of(call: Callable[[], Any]) -> Optional[APIErrorCode]:
    """The code the raised exception names itself, not the one its message would give."""
    with pytest.raises(BaseException) as caught:
        call()
    return getattr(caught.value, "error_code", None)


def test_a_login_email_for_a_vpn_provider_is_a_proxy_error() -> None:
    assert code_of(lambda: NordVPN("user@example.com", "secret")) == APIErrorCode.INVALID_PROXY
    assert code_of(lambda: SurfsharkVPN("user@example.com", "secret")) == APIErrorCode.INVALID_PROXY


def test_gluetun_without_docker_is_a_proxy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "Docker", None)
    assert code_of(Gluetun) == APIErrorCode.INVALID_PROXY


def test_a_provider_that_failed_to_load_or_is_unknown_is_a_proxy_error() -> None:
    providers = [Unavailable("NordVPN", ValueError("bad credentials"))]
    assert code_of(lambda: find_provider(providers, "nordvpn")) == APIErrorCode.INVALID_PROXY
    assert code_of(lambda: find_provider(providers, "other")) == APIErrorCode.INVALID_PROXY


def test_a_query_no_provider_answers_is_a_proxy_error() -> None:
    assert code_of(lambda: pick_proxy([], "gb")) == APIErrorCode.INVALID_PROXY


@pytest.mark.parametrize("proxied, failures", [(None, 99), ({"ip": "1.1.1.1", "country": "gb"}, 0)])
def test_a_failed_proxy_check_is_a_proxy_error(monkeypatch: pytest.MonkeyPatch, proxied: Any, failures: int) -> None:
    def lookup(session: Any = None, cached: bool = False, errors: Any = None) -> Any:
        if errors is not None:
            errors.extend(OSError("refused") for _ in range(failures))
        return proxied if session is not None and not cached else {"ip": "1.1.1.1"}

    monkeypatch.setattr(ip_info, "get_ip_info", lookup)
    assert code_of(lambda: ip_info.verify_proxy_exit(SimpleNamespace())) == APIErrorCode.INVALID_PROXY


class Blocked(Service):
    GEOBLOCK = ("gb",)

    def get_titles(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def get_tracks(self, title):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def get_chapters(self, title):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest.mark.parametrize("proxy", [None, "https://u:p@chosen.example:89"], ids=["own-region", "proxy-region"])
def test_a_blocked_region_is_a_geofence_error(monkeypatch: pytest.MonkeyPatch, proxy: Optional[str]) -> None:
    monkeypatch.setattr(service_mod, "get_ip_info", lambda *args, **kwargs: {"country": "gb"})
    monkeypatch.setattr(service_mod, "verify_proxy_exit", lambda session: {"country": "gb"})
    monkeypatch.setattr(run_proxy, "watched", {})
    monkeypatch.setattr(run_proxy, "proxy_hosts", set())
    ctx = SimpleNamespace(
        obj=SimpleNamespace(config={}, proxy_providers=[]), parent=SimpleNamespace(params={"proxy": proxy})
    )
    assert code_of(lambda: Blocked(ctx)) == APIErrorCode.GEOFENCE  # type: ignore[arg-type]


def test_a_login_that_needs_an_absent_user_is_an_auth_error() -> None:
    assert code_of(lambda: NoUserBridge().request_input("Enter OTP")) == APIErrorCode.AUTH_FAILED
    assert code_of(NoUserBridge().ensure_active) == APIErrorCode.AUTH_FAILED
    assert code_of(lambda: InputBridge().request_input("Enter OTP", timeout=0.05)) == APIErrorCode.AUTH_FAILED


def test_an_import_key_the_export_lacks_is_a_drm_error() -> None:
    assert ImportService.missing_key(SimpleNamespace(drm=None)).error_code == APIErrorCode.DRM_ERROR  # type: ignore[arg-type,attr-defined]


@pytest.mark.parametrize(
    "error",
    [
        Widevine.Exceptions.PSSHNotFound,
        Widevine.Exceptions.KIDNotFound,
        PlayReady.Exceptions.PSSHNotFound,
        PlayReady.Exceptions.KIDNotFound,
        PlayReady.Exceptions.DeviceRevoked,
        ClearKeyCENC.Exceptions.KIDNotFound,
        MonaLisa.Exceptions.TicketNotFound,
        MonaLisa.Exceptions.KeyExtractionFailed,
    ],
)
def test_a_drm_exception_class_is_a_drm_error(error: type[Exception]) -> None:
    assert categorize_exception(error("PSSH was not found in track data.")).error_code == APIErrorCode.DRM_ERROR


def test_a_missing_decrypter_is_a_drm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "Mp4decrypt", None)
    monkeypatch.setattr(binaries, "ShakaPackager", None)
    assert code_of(lambda: decrypters.mp4decrypt(Path("x.mp4"), lambda kids: [])) == APIErrorCode.DRM_ERROR
    assert code_of(lambda: decrypters.shaka_decrypt(Path("x.mp4"), "", "Widevine")) == APIErrorCode.DRM_ERROR


def http_failure(status: Any) -> requests.ConnectionError:
    return requests.ConnectionError("Failed to request the MPD document.", response=SimpleNamespace(status_code=status))


@pytest.mark.parametrize(
    "failure, code",
    [
        (http_failure(404), APIErrorCode.NOT_FOUND),
        (http_failure(410), APIErrorCode.NOT_FOUND),
        (http_failure(429), APIErrorCode.RATE_LIMITED),
        (http_failure(503), APIErrorCode.SERVICE_UNAVAILABLE),
        (http_failure(403), None),
        (http_failure(SimpleNamespace()), None),
        (requests.ConnectionError("dns"), None),
    ],
    ids=["404", "410", "429", "503", "403", "not-a-status", "no-answer"],
)
def test_a_manifest_fetch_failure_is_coded_from_an_unambiguous_status(
    failure: Exception, code: Optional[APIErrorCode]
) -> None:
    error = manifest_fetch_error("DASH", "Example Movie", failure)
    assert isinstance(error, click.ClickException) and "may have expired" in error.message
    assert getattr(error, "error_code", None) == code


def test_a_manifest_fetch_failure_names_the_http_status() -> None:
    assert "(HTTP 403)" in manifest_fetch_error("DASH", "Example Movie", http_failure(403)).message


def test_a_manifest_status_behind_exhausted_retries_is_read_from_the_cause() -> None:
    exhausted = RuntimeError("Max retries exceeded")
    exhausted.__cause__ = http_failure(503)
    error = manifest_fetch_error("DASH", "Example Movie", exhausted)
    assert "(HTTP 503)" in error.message and getattr(error, "error_code", None) == APIErrorCode.SERVICE_UNAVAILABLE
