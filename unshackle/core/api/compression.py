"""aiohttp middleware for gzip transport compression."""

from __future__ import annotations

import gzip

from aiohttp import web


@web.middleware
async def compression_middleware(request: web.Request, handler) -> web.Response:
    """Compress JSON responses with gzip when the client supports it."""
    response = await handler(request)

    accept_encoding = request.headers.get("Accept-Encoding", "")
    if "gzip" not in accept_encoding:
        return response

    if response.content_type and "json" not in response.content_type:
        return response

    body = response.body
    if body is None or len(body) < 256:
        return response

    from unshackle.core.config import config

    level = config.serve.get("compression_level", 1)
    if not level:
        return response

    compressed = gzip.compress(body, compresslevel=level)
    headers = dict(response.headers)
    headers["Content-Encoding"] = "gzip"
    headers["Content-Length"] = str(len(compressed))
    return web.Response(
        body=compressed,
        status=response.status,
        headers=headers,
    )
