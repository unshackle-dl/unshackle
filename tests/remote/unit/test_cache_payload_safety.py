import base64
import datetime as dt
import logging
import zlib
from types import SimpleNamespace

import pytest

from unshackle.core.api import handlers
from unshackle.core.api.sanitize import safe_cache_payload
from unshackle.core.cacher import Cacher
from unshackle.core.config import config
from unshackle.core.remote_service import RemoteService

pytestmark = pytest.mark.unit

TAG = "TESTSVC"


def b64z(text: str) -> str:
    return base64.b64encode(zlib.compress(text.encode())).decode("ascii")


@pytest.mark.parametrize("text", ['{"a": [1, "b", null]}', '"token"'])
def test_plain_json_is_safe(text) -> None:
    assert safe_cache_payload(text)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"py/object": "collections.OrderedDict"}',
        '{"py/type": "builtins.dict"}',
        '{"py/function": "builtins.print"}',
        '{"py/repr": "builtins/1"}',
        '{"py/reduce": [{"py/function": "builtins.print"}, {"py/tuple": ["x"]}]}',
        '{"py/object": "datetime.datetime", "__reduce__": [{"py/type": "collections.OrderedDict"}, []]}',
        '{"py/object": "datetime.datetime", "py/state": {}}',
        pytest.param("[" * 100000 + "]" * 100000, id="deep-nesting"),
    ],
)
def test_unsafe_payload_is_refused(text) -> None:
    assert safe_cache_payload(text) is False


def save_returned(cache_map: dict) -> None:
    RemoteService.save_returned_cache(SimpleNamespace(service_tag=TAG, log=logging.getLogger("test")), cache_map)


@pytest.mark.parametrize(
    "write", [lambda m: handlers.write_client_cache(m, TAG), save_returned], ids=["server", "client"]
)
def test_writer_skips_unsafe_entry(tmp_path, monkeypatch, write) -> None:
    monkeypatch.setattr(config.directories, "cache", tmp_path)
    write({"good": b64z('{"a": 1}'), "bad": b64z('{"py/function": "builtins.print"}')})
    assert (tmp_path / TAG / "good.json").exists()
    assert not (tmp_path / TAG / "bad.json").exists()


@pytest.mark.parametrize(
    "expiration",
    [None, dt.datetime(2030, 1, 1, 12, 0, 0), dt.datetime(2030, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)],
    ids=["none", "naive", "aware"],
)
def test_real_cacher_file_round_trips(tmp_path, monkeypatch, expiration) -> None:
    monkeypatch.setattr(config.directories, "cache", tmp_path / "src")
    cache = Cacher(TAG, "tok")
    cache.set({"token": "abc", "n": [1, 2]}, expiration=expiration)
    text = cache.path.read_text(encoding="utf8")

    monkeypatch.setattr(config.directories, "cache", tmp_path / "dst")
    save_returned({"tok": b64z(text)})
    assert Cacher(TAG).get("tok").data == {"token": "abc", "n": [1, 2]}
