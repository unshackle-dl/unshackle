"""Each title source states how its content keys reach dl, what its caches file it under, and whose login it saves."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import requests

from tests.core.test_export import crit_export, import_ctx
from tests.lang_selection import test_kind_only_selection as harness
from tests.lang_selection.test_kind_only_selection import FakeService
from tests.remote.key_source import MemoryKeySource, remote_keys
from unshackle.commands import dl as dl_module
from unshackle.core.drm import PlayReady, Widevine
from unshackle.core.drm.cenc import drm_stub
from unshackle.core.import_service import ImportService
from unshackle.core.service import Service
from unshackle.core.title_cacher import get_account_hash
from unshackle.core.title_source import CacheIdentity, KeyArrival

KIDS = [UUID(int=1).hex, UUID(int=2).hex]


class ExampleService(Service):
    def get_titles(self) -> Any: ...

    def get_tracks(self, title: Any) -> Any: ...

    def get_chapters(self, title: Any) -> Any: ...


def service(**attrs: Any) -> ExampleService:
    """An ExampleService with only ``attrs`` set, as ``Service.__init__`` needs a config and a network."""
    svc = object.__new__(ExampleService)
    svc.__dict__.update(attrs)
    return svc


def an_import(tmp_path: Path) -> ImportService:
    return ImportService(import_ctx(), "EXAMPLE", "movie-1", str(crit_export(tmp_path / "export.json")))


def test_a_service_licenses_through_its_callbacks_and_has_no_key_source() -> None:
    svc = service()
    assert (svc.key_arrival, svc.key_source) == (KeyArrival.CALLBACK, None)


@pytest.mark.parametrize(("server_cdm", "arrival"), [(True, KeyArrival.SERVER), (False, KeyArrival.CALLBACK)])
def test_a_remote_session_follows_the_server_cdm_and_is_its_own_key_source(
    server_cdm: bool, arrival: KeyArrival
) -> None:
    """Without the server CDM the challenge relay licenses, but the server vault still answers through the session."""
    remote = remote_keys(server_cdm=server_cdm)
    assert (remote.key_arrival, remote.key_source) == (arrival, remote)


def test_an_import_takes_the_keys_from_the_export(tmp_path: Path) -> None:
    imported = an_import(tmp_path)
    assert (imported.key_arrival, imported.key_source) == (KeyArrival.EXPORT, imported)


@pytest.mark.parametrize("attr", ["title", "title_id"])
def test_a_service_is_cached_under_the_title_id_it_keeps(attr: str) -> None:
    svc = service(**{attr: "show-1", "title_cache": "cacher", "current_region": "gb"})
    assert svc.cache_identity() == CacheIdentity("cacher", "show-1", "gb", get_account_hash(None))


def test_a_service_with_no_title_id_has_none_to_cache_under() -> None:
    assert service().cache_identity().title_id is None


def test_a_remote_session_and_an_import_have_no_title_cache(tmp_path: Path) -> None:
    remote = remote_keys()
    remote.title_id = "show-1"
    assert remote.cache_identity() == CacheIdentity(None, "show-1", None, None)
    assert an_import(tmp_path).cache_identity() == CacheIdentity(None, "movie-1", None, None)


def test_only_a_service_saves_its_login_cookies(tmp_path: Path) -> None:
    session = requests.Session()
    assert service(session=session).login_cookies is session.cookies
    assert remote_keys().login_cookies is None
    assert an_import(tmp_path).login_cookies is None


def test_the_source_defaults_leave_a_track_alone() -> None:
    """A source that defines no hooks, sample decrypter or licence callback gets the no-op base."""
    svc = service(log=logging.getLogger("test"))
    assert not svc.decrypts_samples(None)  # type: ignore[arg-type]
    assert svc.get_widevine_license(challenge=b"", title=None, track=None) is None  # type: ignore[arg-type]
    assert svc.get_playready_license(challenge=b"", title=None, track=None) is None  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        svc.get_sample_decrypter(None, 1)  # type: ignore[arg-type]
    svc.close()


def test_a_playready_stub_names_every_kid() -> None:
    stub = drm_stub("playready", KIDS)
    assert isinstance(stub, PlayReady)
    assert [kid.hex for kid in stub.kids] == KIDS


@pytest.mark.parametrize("drm_type", ["widevine", "other"])
def test_any_other_stub_is_widevine_and_names_every_kid(drm_type: str) -> None:
    stub = drm_stub(drm_type, KIDS)
    assert isinstance(stub, Widevine)
    assert [kid.hex for kid in stub.pssh.key_ids] == KIDS


class KeyedSource(FakeService):
    """A harness source that states its key arrival and records what dl does with it at the end of a run."""

    def __init__(self, arrival: KeyArrival, cookies: Any = None) -> None:
        super().__init__({1: {}})
        self.arrival = arrival
        self.cookies = cookies
        self.keys = MemoryKeySource()
        self.keys.key_arrival = arrival
        self.closed = 0

    @property
    def key_arrival(self) -> KeyArrival:
        return self.arrival

    @property
    def key_source(self) -> Any:
        return None if self.arrival is KeyArrival.CALLBACK else self.keys

    @property
    def login_cookies(self) -> Any:
        return self.cookies

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize(
    ("arrival", "line"),
    [
        (KeyArrival.EXPORT, "Using keys from the export; no CDM required"),
        (KeyArrival.SERVER, "Using server CDM (Widevine); no local CDM required"),
    ],
)
def test_dl_licenses_with_no_cdm_of_its_own_when_the_keys_arrive_from_the_source(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, arrival: KeyArrival, line: str
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(logging.getLogger("test_kind_only"), "info", lambda msg, *a, **k: lines.append(msg))
    saved: list[Any] = []
    monkeypatch.setattr(dl_module.login, "save_cookies", lambda *a: saved.append(a))
    source = KeyedSource(arrival)
    assert harness.run(monkeypatch, caplog, {1: {}}, service=source, to_end=True, skip_dl=True) is None
    assert line in lines
    assert (source.closed, saved) == (1, [])


def test_dl_saves_the_login_cookies_of_a_service(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    jar = requests.cookies.RequestsCookieJar()
    saved: list[Any] = []
    monkeypatch.setattr(dl_module.login, "get_cookie_path", lambda *a: tmp_path / "cookies.txt")
    monkeypatch.setattr(dl_module.login, "get_cookie_jar", lambda *a: None)
    monkeypatch.setattr(dl_module.login, "save_cookies", lambda path, cookies: saved.append(cookies))
    source = KeyedSource(KeyArrival.CALLBACK, cookies=jar)
    assert harness.run(monkeypatch, caplog, {1: {}}, service=source, to_end=True, skip_dl=True) is None
    assert saved == [jar]
    assert source.closed == 1
