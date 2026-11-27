"""An rnet response works in a ``with`` block, as a requests.Response does.

The music tagger reads a cover with ``with session.get(...)`` on the session that the service gives it.
Each test here sends a real rnet request to a local server.
The body has no image magic bytes, so a content type in a result comes only from the response header.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from unshackle.core.music.tagger import download_cover
from unshackle.core.session import RnetSession

pytestmark = pytest.mark.unit

COVER = b"cover body"


class CoverServer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(COVER)))
        self.end_headers()
        self.wfile.write(COVER)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def cover_url() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), CoverServer)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/cover.png"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_with_block_gives_the_response_itself_and_its_content(cover_url: str) -> None:
    response = RnetSession().get(cover_url)

    with response as entered:
        assert entered is response
        assert entered.content == COVER


def test_the_end_of_the_with_block_closes_the_response(cover_url: str) -> None:
    with RnetSession().get(cover_url) as response:
        pass

    with pytest.raises(RuntimeError):
        _ = response.content


def test_an_exception_in_the_with_block_propagates_and_the_response_still_closes(cover_url: str) -> None:
    with pytest.raises(ValueError, match="raised inside the with block"), RnetSession().get(cover_url) as response:
        raise ValueError("raised inside the with block")

    with pytest.raises(RuntimeError):
        _ = response.content


def test_download_cover_returns_the_image_and_its_content_type(cover_url: str) -> None:
    assert download_cover(RnetSession(), cover_url) == (COVER, "image/png")
