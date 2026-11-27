"""Over a remote session, the content keys a decrypt proved go back to the server, which keeps only what it lacks.

Over ``--cdm-only`` the session runs on the client's own device and asks the server for no key.
"""

import asyncio
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import click
import pytest
from pywidevine.pssh import PSSH

from tests.core.key_resolver import key_table, resolver
from tests.remote.key_source import remote_keys
from tests.remote.sessions import session_entry
from unshackle.core import remote_channel
from unshackle.core.api import handlers
from unshackle.core.cdm.detect import cdm_type_stub
from unshackle.core.config import config
from unshackle.core.drm import Widevine, key_resolution
from unshackle.core.remote_service import RemoteService
from unshackle.core.title_source import KeyArrival
from unshackle.core.vaults import Vaults
from unshackle.vaults.SQLite import SQLite

pytestmark = pytest.mark.unit

LICENSED = UUID("11111111-2222-3333-4444-555555555555")
VAULTED = UUID("22222222-2222-3333-4444-555555555555")
SERVED = UUID("33333333-2222-3333-4444-555555555555")
HELD = UUID("44444444-2222-3333-4444-555555555555")
FLAGGED = UUID("55555555-2222-3333-4444-555555555555")
UNNAMED = UUID("66666666-2222-3333-4444-555555555555")
LATE = UUID("77777777-2222-3333-4444-555555555555")
NULL = UUID("88888888-2222-3333-4444-555555555555")
KEY = "ab" * 16
OTHER_KEY = "cd" * 16


class Remote(SQLite):
    """A second SQLite file standing in for a remote vault."""

    local = False


def client(posts: list) -> RemoteService:
    svc = remote_keys(server_cdm=False)
    svc._session_id = "sess"
    svc._track_alias = {"v1": "v9"}
    svc.client = SimpleNamespace(
        send=lambda method, endpoint, timeout, background, json: posts.append((method, endpoint, background, json))
    )
    return svc


def test_the_server_vault_posts_only_the_pairs_the_server_did_not_serve() -> None:
    posts: list = []
    svc = client(posts)
    svc.note_vault_keys({SERVED.hex: KEY}, set())

    assert svc.server_vault.add_keys("EXAMPLE", {LICENSED: KEY, SERVED: KEY}, SimpleNamespace(id="v1")) == 0
    svc.server_vault.add_keys("EXAMPLE", {SERVED: KEY}, SimpleNamespace(id="v1"))

    assert posts == [("post", "/api/session/sess/keys", True, {"track_id": "v9", "keys": {LICENSED.hex: KEY}})]


def test_a_pair_the_server_accepted_is_not_posted_again() -> None:
    posts: list = []
    svc = client(posts)
    svc.client.send = lambda method, endpoint, timeout, background, json: posts.append(json) or SimpleNamespace(ok=True)

    svc.server_vault.add_keys("EXAMPLE", {LICENSED: KEY}, SimpleNamespace(id="v1"))
    svc.server_vault.add_keys("EXAMPLE", {LICENSED: KEY, VAULTED: KEY}, SimpleNamespace(id="v1"))

    assert [post["keys"] for post in posts] == [{LICENSED.hex: KEY}, {VAULTED.hex: KEY}]


def test_every_key_of_a_licence_goes_to_the_server_vault_without_a_decrypt(monkeypatch) -> None:
    licensed = {LICENSED: KEY, LATE: OTHER_KEY}
    monkeypatch.setattr(Widevine, "get_content_keys", lambda self, **_: setattr(self, "content_keys", dict(licensed)))
    pushed: list = []
    source = remote_keys(server_cdm=False)
    source.server_vault = SimpleNamespace(add_keys=lambda service, keys, track: pushed.append((keys, track)))
    keys = resolver(tag="EXAMPLE", source=source)
    drm = Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[LICENSED]))
    track = SimpleNamespace(id="v1", drm=[drm])

    keys.licence_for(track, None, key_table())(drm, track_kid=LICENSED)
    keys.wait_vault_writes()

    assert pushed == [(licensed, track)]


def test_a_failed_post_is_dropped_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    svc = client([])

    def refused(method: str, endpoint: str, timeout: float, background: bool, json: Any) -> None:
        raise ConnectionError("EXAMPLE server down")

    svc.client.send = refused
    svc.server_vault.add_keys("EXAMPLE", {LICENSED: KEY}, SimpleNamespace(id="v1"))

    assert caplog.records == []


def test_every_served_pair_is_remembered_whatever_its_source() -> None:
    svc = remote_keys()
    svc.note_vault_keys({VAULTED.hex: KEY, SERVED.hex: OTHER_KEY}, {VAULTED.hex})
    assert svc.served_keys == {(VAULTED, KEY), (SERVED, OTHER_KEY)}


def decrypt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ffmpeg: Any,
    decodes: bool = True,
    track: Any = "track",
    flagged: dict | None = None,
    licence: Any = None,
) -> list:
    """Decrypt a track that holds a licence key, a vault key and a null key; return what reached the server vault."""
    monkeypatch.setattr(key_resolution.binaries, "FFMPEG", ffmpeg)
    monkeypatch.setattr(key_resolution, "ffmpeg_decodes", lambda path, start=None, seconds=3, video=None: decodes)
    local = SQLite("local", tmp_path / "local.db")
    local.add_key("EXAMPLE", VAULTED, OTHER_KEY)
    vaults = Vaults("EXAMPLE")
    vaults.vaults = [local]
    assert vaults.get_key(VAULTED) == (OTHER_KEY, local)
    pushed: list = []
    source = SimpleNamespace(
        key_arrival=KeyArrival.CALLBACK,
        server_vault=SimpleNamespace(add_keys=lambda service, keys, track: pushed.append((keys, track))),
    )
    keys = resolver(vaults, tag="EXAMPLE", source=source)
    for kid, key in (flagged or {}).items():
        vaults.flag_bad_key(kid, key)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    content_keys = {LICENSED: KEY, VAULTED: OTHER_KEY, NULL: "0" * 32, **(flagged or {})}
    drm = SimpleNamespace(content_keys=content_keys, decrypt=lambda path: None)
    try:
        keys.decrypt_verified(drm, path, licence, LICENSED, track=track)
    finally:
        keys.wait_vault_writes()
    return pushed


def test_a_passed_decode_sends_the_licence_keys_and_the_checked_vault_keys(tmp_path, monkeypatch) -> None:
    assert decrypt(tmp_path, monkeypatch, "ffmpeg") == [({LICENSED: KEY, VAULTED: OTHER_KEY}, "track")]


def test_without_ffmpeg_a_vault_key_is_not_sent(tmp_path, monkeypatch) -> None:
    assert decrypt(tmp_path, monkeypatch, None) == [({LICENSED: KEY}, "track")]


def test_a_failed_decode_sends_nothing(tmp_path, monkeypatch) -> None:
    with pytest.raises(ValueError):
        decrypt(tmp_path, monkeypatch, "ffmpeg", decodes=False)


def test_a_flagged_pair_is_replaced_before_anything_is_sent(tmp_path, monkeypatch) -> None:
    def licence(drm: Any, track_kid: UUID) -> None:
        drm.content_keys[FLAGGED] = OTHER_KEY

    pushed = decrypt(tmp_path, monkeypatch, "ffmpeg", flagged={FLAGGED: KEY}, licence=licence)
    assert pushed == [({LICENSED: KEY, VAULTED: OTHER_KEY, FLAGGED: OTHER_KEY}, "track")]


def test_a_decrypt_without_a_track_sends_nothing(tmp_path, monkeypatch) -> None:
    assert decrypt(tmp_path, monkeypatch, "ffmpeg", track=None) == []


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """A remote session whose tracks name every KID but UNNAMED, over a local vault and a remote one."""
    late = SimpleNamespace(id="v2", drm=None)
    tracks = {
        "v1": SimpleNamespace(id="v1", drm=[SimpleNamespace(kids=[LICENSED, SERVED, HELD, FLAGGED])]),
        "v2": late,
    }
    entry = session_entry(service_tag="EXAMPLE", tracks=tracks)
    entry.served_keys[SERVED.hex] = (KEY, "cdm")
    local = SQLite("local", tmp_path / "local.db")
    remote = Remote("remote", tmp_path / "remote.db")
    remote.add_key("EXAMPLE", HELD, OTHER_KEY)
    vaults = Vaults("EXAMPLE")
    vaults.vaults = [local, remote]
    vaults.flag_bad_key(FLAGGED, KEY)
    ensured: list = []

    async def validated(session_id: str, request: Any) -> Any:
        return entry

    def ensure(track: Any, session: Any = None, init_data: Any = None) -> None:
        ensured.append(track.id)
        track.drm = [SimpleNamespace(kids=[LATE])]

    monkeypatch.setattr(handlers, "get_validated_session", validated)
    monkeypatch.setattr(handlers, "require_authenticated", lambda session: None)
    monkeypatch.setattr(handlers, "load_server_vaults", lambda name: vaults)
    monkeypatch.setattr(handlers, "ensure_track_drm", ensure)
    return SimpleNamespace(local=local, remote=remote, ensured=ensured)


async def settled() -> None:
    """Wait for the push to the remote vaults, which runs after the answer."""
    await asyncio.get_running_loop().shutdown_default_executor()


async def test_the_server_stores_only_a_named_kid_no_local_vault_holds(server, caplog) -> None:
    caplog.set_level(logging.INFO, logger="api")
    posted = {kid.hex: KEY for kid in (LICENSED, SERVED, HELD, FLAGGED, UNNAMED, LATE)}

    resp = await handlers.session_keys_handler({"track_id": "v2", "keys": {**posted, "nope": "zz"}}, "sess")

    await settled()

    assert (resp.status, json.loads(resp.body)) == (200, {})
    assert server.ensured == ["v2"]
    for vault in (server.local, server.remote):
        assert vault.get_key(LICENSED, "EXAMPLE") == KEY
        assert vault.get_key(LATE, "EXAMPLE") == KEY
        assert vault.get_key(UNNAMED, "EXAMPLE") is None
        assert vault.get_key(SERVED, "EXAMPLE") is None
    assert server.remote.get_key(HELD, "EXAMPLE") == OTHER_KEY
    assert server.local.get_key(HELD, "EXAMPLE") == KEY
    assert server.local.is_bad_key(FLAGGED, KEY)
    assert [r.getMessage() for r in caplog.records if r.name == "api"] == [
        "Session sess sent 3 client content key(s) to the server vaults"
    ]


async def test_the_server_answers_before_the_remote_vaults_take_the_keys(server, monkeypatch) -> None:
    release = threading.Event()
    real = server.remote.add_keys

    def slow(service: str, kid_keys: dict) -> int:
        release.wait(10)
        return real(service, kid_keys)

    monkeypatch.setattr(server.remote, "add_keys", slow)
    await handlers.session_keys_handler({"track_id": "v2", "keys": {LICENSED.hex: KEY}}, "sess")

    assert server.local.get_key(LICENSED, "EXAMPLE") == KEY
    assert server.remote.get_key(LICENSED, "EXAMPLE") is None
    release.set()
    await settled()
    assert server.remote.get_key(LICENSED, "EXAMPLE") == KEY


async def test_a_local_vault_that_cannot_answer_counts_as_holding_the_kid(server, monkeypatch) -> None:
    real = server.local.get_key

    def down(kid: Any, service: str) -> Any:
        if kid == LICENSED:
            raise OSError("EXAMPLE vault file locked")
        return real(kid, service)

    monkeypatch.setattr(server.local, "get_key", down)
    await handlers.session_keys_handler({"track_id": "v2", "keys": {LICENSED.hex: KEY, LATE.hex: KEY}}, "sess")
    await settled()
    assert server.remote.get_key(LICENSED, "EXAMPLE") is None
    assert server.remote.get_key(LATE, "EXAMPLE") == KEY


async def test_the_server_answers_empty_when_it_stores_nothing(server, caplog) -> None:
    caplog.set_level(logging.INFO, logger="api")
    resp = await handlers.session_keys_handler({"track_id": ["v1"], "keys": {UNNAMED.hex: KEY}}, "sess")
    assert (resp.status, json.loads(resp.body)) == (200, {})
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


def cdm_only_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cdm: Any, created: dict) -> RemoteService:
    """A real RemoteService for a ``--cdm-only`` run whose server answers session create with ``created``."""
    monkeypatch.setattr(config, "credentials", {})
    monkeypatch.setattr(config.directories, "cookies", tmp_path / "cookies")
    monkeypatch.setattr(config.directories, "cache", tmp_path / "cache")
    monkeypatch.setattr(
        remote_channel,
        "open_channel",
        lambda service: SimpleNamespace(
            in_flight=None, close=lambda: None, drain_logs=lambda: None, keep_alive=lambda: None
        ),
    )
    obj = SimpleNamespace(cdm=cdm, cdm_entry=False, proxy_providers=[])
    parent = click.Context(click.Command("dl"), obj=obj)
    parent.params = {"no_proxy": True, "cdm_only": True}
    ctx = click.Context(click.Command("EXAMPLE"), parent=parent, obj=obj)
    svc = RemoteService(ctx, "EXAMPLE", "example-1", "http://server", "key", {"_server_cdm": True})
    posts: list = []
    svc.client = SimpleNamespace(
        tunnel=None, post=lambda endpoint, data, expect=(): posts.append(data) or created, delete=lambda endpoint: {}
    )
    svc.posts = posts
    return svc


def test_cdm_only_declines_the_server_cdm_and_its_vault(monkeypatch, tmp_path) -> None:
    created = {"session_id": "sess", "server_cdm": False, "server_vault": True}
    svc = cdm_only_client(monkeypatch, tmp_path, cdm_type_stub("widevine"), created)
    try:
        svc.authenticate()
        assert svc.posts[0]["server_cdm"] is False
        assert (svc.key_arrival, svc._server_vault) == (KeyArrival.CALLBACK, False)
        svc.resolve_server_keys(SimpleNamespace(tracks=SimpleNamespace(videos=["v"], audio=[])))
        assert len(svc.posts) == 1
    finally:
        svc.close()


def test_cdm_only_without_a_local_cdm_fails_before_the_session(monkeypatch, tmp_path) -> None:
    svc = cdm_only_client(monkeypatch, tmp_path, None, {})
    with pytest.raises(click.ClickException, match="needs a local CDM for EXAMPLE"):
        svc.authenticate()
    assert svc.posts == []


def test_cdm_only_on_the_server_device_fails(monkeypatch, tmp_path) -> None:
    created = {"session_id": "sess", "server_cdm": True, "server_device": True}
    svc = cdm_only_client(monkeypatch, tmp_path, cdm_type_stub("widevine"), created)
    try:
        with pytest.raises(click.ClickException, match="runs on the server's device"):
            svc.authenticate()
    finally:
        svc.close()
