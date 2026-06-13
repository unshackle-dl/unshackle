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


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_skips_when_client_does_not_accept_gzip() -> None:
    body_json = json.dumps({"data": "x" * 4096}).encode()

    async def handler(req):  # noqa: ARG001
        return web.json_response({"data": "x" * 4096})

    req = _FakeReq(accept_encoding="identity")
    resp = _run(compression_middleware(req, handler))
    assert resp.headers.get("Content-Encoding") != "gzip"
    assert resp.body == body_json or len(resp.body) >= len(body_json) - 8


def test_skips_when_body_below_threshold() -> None:
    async def handler(req):  # noqa: ARG001
        return web.json_response({"hi": "x"})

    resp = _run(compression_middleware(_FakeReq(), handler))
    assert resp.headers.get("Content-Encoding") != "gzip"


def test_skips_non_json_response() -> None:
    async def handler(req):  # noqa: ARG001
        return web.Response(body=b"x" * 4096, content_type="text/plain")

    resp = _run(compression_middleware(_FakeReq(), handler))
    assert resp.headers.get("Content-Encoding") != "gzip"


def test_compresses_large_json_when_accepted() -> None:
    big = {"data": "x" * 4096}

    async def handler(req):  # noqa: ARG001
        return web.json_response(big)

    resp = _run(compression_middleware(_FakeReq(), handler))
    assert resp.headers.get("Content-Encoding") == "gzip"
    decompressed = gzip.decompress(resp.body)
    assert json.loads(decompressed) == big
    assert resp.headers["Content-Length"] == str(len(resp.body))
