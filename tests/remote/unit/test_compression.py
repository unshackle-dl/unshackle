"""Unit tests for unshackle.core.api.compression.compression_middleware."""

from __future__ import annotations

import asyncio
import gzip
import json

import pytest
from aiohttp import web

from unshackle.core.api.compression import compression_middleware

pytestmark = pytest.mark.unit


class _FakeReq:
    def __init__(self, accept_encoding: str = "gzip") -> None:
        self.headers = {"Accept-Encoding": accept_encoding}


def run(coro):
    return asyncio.run(coro)


def test_skips_when_client_does_not_accept_gzip() -> None:
    body_json = json.dumps({"data": "x" * 4096}).encode()

    async def handler(req):  # noqa: ARG001
        return web.json_response({"data": "x" * 4096})

    req = _FakeReq(accept_encoding="identity")
    resp = run(compression_middleware(req, handler))
    assert resp.headers.get("Content-Encoding") != "gzip"
    assert resp.body == body_json or len(resp.body) >= len(body_json) - 8


def test_skips_when_body_below_threshold() -> None:
    async def handler(req):  # noqa: ARG001
        return web.json_response({"hi": "x"})

    resp = run(compression_middleware(_FakeReq(), handler))
    assert resp.headers.get("Content-Encoding") != "gzip"


def test_skips_non_json_response() -> None:
    async def handler(req):  # noqa: ARG001
        return web.Response(body=b"x" * 4096, content_type="text/plain")

    resp = run(compression_middleware(_FakeReq(), handler))
    assert resp.headers.get("Content-Encoding") != "gzip"


def test_compresses_large_json_when_accepted() -> None:
    big = {"data": "x" * 4096}

    async def handler(req):  # noqa: ARG001
        return web.json_response(big)

    resp = run(compression_middleware(_FakeReq(), handler))
    assert resp.headers.get("Content-Encoding") == "gzip"
    decompressed = gzip.decompress(resp.body)
    assert json.loads(decompressed) == big
    assert resp.content_length == len(resp.body)


def test_compresses_a_body_over_the_thread_size_on_a_worker_thread(monkeypatch) -> None:
    from unshackle.core.api import compression

    threaded = []
    real = asyncio.to_thread

    async def to_thread(fn, *args, **kwargs):
        threaded.append(fn)
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(compression.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(compression, "GZIP_THREAD_SIZE", 1024)
    big = {"data": "x" * 4096}

    async def handler(req):  # noqa: ARG001
        return web.json_response(big)

    resp = run(compression_middleware(_FakeReq(), handler))
    assert threaded == [gzip.compress]
    assert json.loads(gzip.decompress(resp.body)) == big
