"""List and search requests on a --remote-only server run on a per-request cache directory.

The directory holds only what the client sent, goes back to the client when it logged in
itself, and is always removed when the request ends.
"""

import base64
import json
import zlib

import click
import pytest

from unshackle.core.api import handlers
from unshackle.core.api.stats import stats
from unshackle.core.cacher import Cacher
from unshackle.core.config import config


class _CacheService:
    fail_titles = False

    def __init__(self, ctx, title):
        self.seen = _CacheService.seen
        self.seen["parent_params"] = ctx.parent.params
        self.cache = Cacher("EXAMPLE")

    def authenticate(self, cookies, credential):
        self.seen["seeded"] = self.cache.get("tokens").data
        self.seen["cache_dir"] = self.cache.get("tokens").path.parent
        self.cache.get("tokens").set("new")
        self.cache.get("titles_abc").set("titles")

    def get_titles(self):
        if self.fail_titles:
            raise RuntimeError("boom")
        return []

    def search(self):
        return iter(())


@pytest.fixture
def seen(tmp_path, monkeypatch):
    import types

    _CacheService.seen = {}
    _CacheService.fail_titles = False

    @click.command()
    @click.argument("title")
    @click.pass_context
    def cli(ctx, **kwargs):
        return _CacheService(ctx, **kwargs)

    module = types.SimpleNamespace(cli=cli)
    monkeypatch.setattr(config.directories, "cache", tmp_path)
    monkeypatch.setattr(stats, "mode", "remote_only")
    monkeypatch.setattr(handlers, "validate_service", lambda tag, request=None: tag)
    monkeypatch.setattr(handlers, "load_service_yaml", lambda service: {})
    monkeypatch.setattr(handlers, "resolve_handler_proxy", lambda *args: (None, []))
    monkeypatch.setattr(handlers, "resolve_server_account", lambda *args: None)
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *args: None)
    monkeypatch.setattr(handlers.Services, "load", staticmethod(lambda service: module))
    monkeypatch.setattr("unshackle.commands.dl.dl.get_cookie_jar", staticmethod(lambda service, profile: None))
    monkeypatch.setattr("unshackle.commands.dl.dl.get_credentials", staticmethod(lambda service, profile: None))
    return _CacheService.seen


def client_cache(tmp_path, value):
    Cacher("SRC").get("tokens").set(value)
    text = (tmp_path / "SRC" / "tokens.json").read_bytes()
    return {"tokens": base64.b64encode(zlib.compress(text)).decode("ascii")}


def request_dirs(tmp_path):
    return list((tmp_path / "_requests").rglob("*")) if (tmp_path / "_requests").exists() else []


CREDENTIALS = {"credentials": {"username": "a@example.com", "password": "pw"}}


@pytest.mark.asyncio
async def test_seeded_cache_is_read_and_returned_then_removed(seen, tmp_path):
    data = {"service": "EXAMPLE", "title_id": "t", "cache": client_cache(tmp_path, "old")}
    response = await handlers.list_titles_handler(data)
    body = json.loads(response.body)

    assert seen["seeded"] == "old"
    assert seen["cache_dir"].is_relative_to(tmp_path / "_requests")
    assert set(body["cache"]) == {"tokens"}
    returned = json.loads(zlib.decompress(base64.b64decode(body["cache"]["tokens"])))
    assert returned["data"] == "new"
    assert not seen["cache_dir"].exists()
    assert not request_dirs(tmp_path)


@pytest.mark.asyncio
async def test_search_returns_cache_to_a_client_with_credentials(seen, tmp_path):
    response = await handlers.search_handler({"service": "EXAMPLE", "query": "q", **CREDENTIALS})
    body = json.loads(response.body)
    assert body["count"] == 0
    assert set(body["cache"]) == {"tokens"}
    assert not request_dirs(tmp_path)


@pytest.mark.asyncio
async def test_no_cache_returned_without_client_login(seen, tmp_path):
    response = await handlers.search_handler({"service": "EXAMPLE", "query": "q"})
    assert "cache" not in json.loads(response.body)
    assert seen["cache_dir"].is_relative_to(tmp_path / "_requests")
    assert not request_dirs(tmp_path)


@pytest.mark.asyncio
async def test_cache_dir_removed_on_error(seen, tmp_path):
    _CacheService.fail_titles = True
    data = {"service": "EXAMPLE", "title_id": "t", **CREDENTIALS}
    response = await handlers.list_tracks_handler(data)
    assert response.status >= 400
    assert "cache" not in json.loads(response.body)
    assert seen["cache_dir"].is_relative_to(tmp_path / "_requests")
    assert not request_dirs(tmp_path)


@pytest.mark.asyncio
async def test_invalid_client_cache_still_removes_the_dir(seen, tmp_path):
    data = {"service": "EXAMPLE", "query": "q", "cache": {"tokens": 1}}
    with pytest.raises(handlers.APIError):
        await handlers.search_handler(data)
    assert not request_dirs(tmp_path)


@pytest.mark.asyncio
async def test_server_account_uses_its_own_cache_and_returns_none(seen, tmp_path, monkeypatch):
    monkeypatch.setattr(handlers, "resolve_server_account", lambda *args: "acct")
    monkeypatch.setattr(handlers, "server_account_cookies", lambda *args: None)
    data = {"service": "EXAMPLE", "query": "q", "cache": client_cache(tmp_path, "old"), **CREDENTIALS}
    response = await handlers.search_handler(data)

    assert "cache" not in json.loads(response.body)
    assert not seen["seeded"]
    assert seen["cache_dir"] == tmp_path / "_accounts" / "EXAMPLE" / "acct"
    assert not (tmp_path / "_requests").exists()


@pytest.mark.asyncio
async def test_full_mode_keeps_the_server_cache(seen, tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "mode", "full")
    data = {"service": "EXAMPLE", "query": "q", "cache": client_cache(tmp_path, "old"), **CREDENTIALS}
    response = await handlers.search_handler(data)

    assert "cache" not in json.loads(response.body)
    assert not seen["seeded"]
    assert seen["cache_dir"] == tmp_path / "EXAMPLE"
    assert not (tmp_path / "_requests").exists()


@pytest.mark.asyncio
async def test_list_tracks_service_sees_dl_params(seen):
    from unshackle.core.tracks import Audio

    data = {"service": "EXAMPLE", "title_id": "t", "dl_params": {"a_lang": ["es-419"], "acodec": ["ddp"]}}
    await handlers.list_tracks_handler(data)
    assert seen["parent_params"]["a_lang"] == ["es-419"]
    assert seen["parent_params"]["acodec"] == [Audio.Codec.EC3]
    assert "dl_params" in handlers.LIST_HANDLER_TRANSPORT_KEYS


@pytest.mark.asyncio
async def test_search_service_gets_no_dl_params(seen):
    await handlers.search_handler({"service": "EXAMPLE", "query": "q", "dl_params": {"a_lang": ["es-419"]}})
    assert "a_lang" not in seen["parent_params"]
