import gzip
import importlib
import socket
import threading

import pytest
import requests as rq

from unshackle.core.downloaders.requests import _is_content_encoded, download


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("identity", False),
        ("IDENTITY", False),
        ("identity, identity", False),
        ("gzip", True),
        ("GZIP", True),
        (" gzip ", True),
        ("x-gzip", True),
        ("br", True),
        ("zstd", True),
        ("deflate", True),
        ("compress", True),
        ("dcb", True),
        ("gzip, br", True),
        ("identity, gzip", True),
        ("gzip, identity", True),
        ("gzip,,", True),
        ("some-future-coding", True),
    ],
)
def test_is_content_encoded(value, expected):
    assert _is_content_encoded(value) is expected


PLAIN = b"WEBVTT\n\n" + b"".join(f"{i:05d} cue text\n".encode() for i in range(2000))


class _EncodingServer:
    """Serves one gzip body, honouring Range against the encoded representation."""

    def __init__(self):
        self.encoded = gzip.compress(PLAIN)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/sub.vtt"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                request = conn.recv(65536).decode("latin1")
                start = next(
                    (
                        int(line.split("=", 1)[1].split("-")[0])
                        for line in request.split("\r\n")
                        if line.lower().startswith("range:")
                    ),
                    None,
                )
                if start is None:
                    body = self.encoded
                    head = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
                else:
                    body = self.encoded[start:]
                    head = (
                        f"HTTP/1.1 206 Partial Content\r\nContent-Length: {len(body)}\r\n"
                        f"Content-Range: bytes {start}-{len(self.encoded) - 1}/{len(self.encoded)}\r\n"
                    )
                conn.sendall(
                    (head + "Content-Encoding: gzip\r\nAccept-Ranges: bytes\r\nConnection: close\r\n\r\n").encode()
                    + body
                )
            except Exception:
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


@pytest.fixture
def encoding_server():
    server = _EncodingServer()
    yield server
    server.close()


def test_encoded_body_is_decoded_on_disk(tmp_path, encoding_server):
    save_path = tmp_path / "sub.vtt"
    list(download(url=encoding_server.url, save_path=save_path, session=rq.Session()))
    assert save_path.read_bytes() == PLAIN


def test_encoded_body_discards_a_partial_instead_of_range_resuming(tmp_path, encoding_server):
    save_path = tmp_path / "sub.vtt"
    save_path.with_name("sub.vtt.!dev").write_bytes(PLAIN[:5000])
    list(download(url=encoding_server.url, save_path=save_path, session=rq.Session()))
    assert save_path.read_bytes() == PLAIN


class _FakeRnetResponse:
    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = headers
        self.content_length = len(body)
        self._body = body

    def raise_for_status(self):
        pass

    def stream(self):
        yield self._body

    def close(self):
        pass


class _FakeRnetSession:
    """Duck-typed rnet stand-in: an encoded 206 for a Range request (the resume the
    fail-closed guard must reject), an encoded 200 for the clean restart."""

    def __init__(self, body):
        self.body = body
        self.range_requests = 0

    def get(self, url, stream=True, **kwargs):
        headers = kwargs.get("headers") or {}
        if any(k.lower() == "range" for k in headers):
            self.range_requests += 1
            return _FakeRnetResponse(206, {"Content-Encoding": "zstd"}, b"\x00mid-stream-slice\x00")
        return _FakeRnetResponse(200, {"Content-Encoding": "zstd"}, self.body)


def test_rnet_encoded_body_discards_a_partial_instead_of_range_resuming(tmp_path, monkeypatch):
    dl = importlib.import_module("unshackle.core.downloaders.requests")
    monkeypatch.setattr(dl, "_is_rnet_session", lambda s: isinstance(s, _FakeRnetSession))
    session = _FakeRnetSession(PLAIN)
    save_path = tmp_path / "sub.vtt"
    save_path.with_name("sub.vtt.!dev").write_bytes(PLAIN[:5000])
    list(download(url="http://127.0.0.1:1/sub.vtt", save_path=save_path, session=session))
    # the encoded 206 must be dropped (never appended); the restart fetches the whole body
    assert session.range_requests == 1
    assert save_path.read_bytes() == PLAIN
