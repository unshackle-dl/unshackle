"""aiohttp middleware for gzip transport compression."""

from __future__ import annotations

import gzip
import zlib

from aiohttp import web

# Cap the inflated size of a peer-supplied compressed blob (cookies, session cache,
# serialized manifests) so a tiny payload cannot expand without bound (zip-bomb DoS).
MAX_INFLATE_BYTES = 64 * 1024 * 1024  # 64 MiB


def safe_inflate(data: bytes, max_size: int = MAX_INFLATE_BYTES) -> bytes:
    """zlib-decompress *data*, raising ValueError if the result would exceed *max_size*."""
    decompressor = zlib.decompressobj()
    out = decompressor.decompress(data, max_size + 1)
    if len(out) > max_size or decompressor.unconsumed_tail:
        raise ValueError("compressed payload expands beyond allowed size")
    out += decompressor.flush()
    if len(out) > max_size:
        raise ValueError("compressed payload expands beyond allowed size")
    return out


@web.middleware
async def compression_middleware(request: web.Request, handler) -> web.StreamResponse:
    """Compress JSON responses with gzip when the client supports it."""
    response = await handler(request)

    if not isinstance(response, web.Response):
        return response

    accept_encoding = request.headers.get("Accept-Encoding", "")
    if "gzip" not in accept_encoding:
        return response

    if response.content_type and "json" not in response.content_type:
        return response

    body = response.body
    if not isinstance(body, (bytes, bytearray)) or len(body) < 256:
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
