import socket
import sys
import threading

import pytest
import requests as rq

from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.downloaders.requests import download, has_range_header
from unshackle.core.downloaders.requests import requests as requests_downloader

# the package re-exports the `requests` function, shadowing the module of the same name
downloader = sys.modules[download.__module__]

BODY = b"".join(f"{i:05d} payload\n".encode() for i in range(500))


class _Server:
    """Serves BODY, with a switch for the two failure modes retries have to handle."""

    def __init__(self, mode):
        self.mode = mode
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/seg.mp4"
        self.requests = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                request = conn.recv(65536).decode("latin1")
                headers = dict(
                    (name.strip().lower(), value.strip())
                    for name, _, value in (line.partition(":") for line in request.split("\r\n"))
                    if value
                )
                self.requests.append(headers)
                ranged = "range" in headers
                if self.mode == "unavailable":
                    conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                elif ranged:
                    conn.sendall(
                        b"HTTP/1.1 416 Range Not Satisfiable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                else:
                    conn.sendall(
                        f"HTTP/1.1 200 OK\r\nContent-Length: {len(BODY)}\r\nConnection: close\r\n\r\n".encode() + BODY
                    )
            except Exception:
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


@pytest.fixture(autouse=True)
def clear_cancel():
    # the batch failure path sets the process-global cancel; leaking it makes every later
    # test that waits on the event (the speed limiter) return instantly
    DOWNLOAD_CANCELLED.clear()
    yield
    DOWNLOAD_CANCELLED.clear()


@pytest.fixture
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(downloader, "RETRY_WAIT", 0)


@pytest.fixture
def server(request, no_retry_wait):
    server = _Server(request.param)
    yield server
    server.close()


@pytest.mark.parametrize("server", ["unavailable"], indirect=True)
def test_exhausted_retries_raise_instead_of_reporting_success(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    with pytest.raises(rq.HTTPError):
        list(download(url=server.url, save_path=save_path, session=rq.Session()))
    assert len(server.requests) == downloader.MAX_ATTEMPTS
    assert not save_path.exists()


@pytest.mark.parametrize("server", ["unavailable"], indirect=True)
def test_cancelled_download_stays_silent(tmp_path, server, monkeypatch):
    # one attempt only, so a cancel check ordered after the exhaustion raise would fail here
    monkeypatch.setattr(downloader, "MAX_ATTEMPTS", 1)
    save_path = tmp_path / "0.mp4"
    DOWNLOAD_CANCELLED.set()
    list(download(url=server.url, save_path=save_path, session=rq.Session()))
    assert not save_path.exists()


@pytest.mark.parametrize("server", ["range_unsatisfiable"], indirect=True)
def test_stale_partial_restarts_clean_on_416(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    save_path.with_name("0.mp4.!dev").write_bytes(b"\x00" * len(BODY))
    list(download(url=server.url, save_path=save_path, session=rq.Session()))
    assert save_path.read_bytes() == BODY
    assert [r.get("range") for r in server.requests] == [f"bytes={len(BODY)}-", None]


@pytest.mark.parametrize("server", ["range_unsatisfiable"], indirect=True)
def test_slice_segment_never_gets_a_resume_range(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    save_path.with_name("0.mp4.!dev").write_bytes(b"\x00" * 128)
    with pytest.raises(OSError):
        list(download(url=server.url, save_path=save_path, session=rq.Session(), headers={"Range": "bytes=0-127"}))
    assert [r["range"] for r in server.requests] == ["bytes=0-127"] * downloader.MAX_ATTEMPTS


@pytest.mark.parametrize("server", ["range_unsatisfiable"], indirect=True)
def test_identity_encoding_requested_unless_the_caller_set_one(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    list(download(url=server.url, save_path=save_path, session=rq.Session()))
    assert server.requests[-1]["accept-encoding"] == "identity"

    list(
        download(
            url=server.url, save_path=tmp_path / "1.mp4", session=rq.Session(), headers={"accept-encoding": "gzip"}
        )
    )
    assert server.requests[-1]["accept-encoding"] == "gzip"


@pytest.mark.parametrize("server", ["unavailable"], indirect=True)
def test_failed_segment_fails_the_batch(tmp_path, server):
    # a segment the batch never delivers must raise, not merge short of the segment count
    with pytest.raises(rq.HTTPError):
        list(
            requests_downloader(
                urls=[server.url, server.url],
                output_dir=tmp_path,
                filename="{i:01}.mp4",
                max_workers=2,
            )
        )
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".mp4") == []


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({}, False),
        ({"headers": None}, False),
        ({"headers": {}}, False),
        ({"headers": {"Accept": "*/*"}}, False),
        ({"headers": {"Range": "bytes=0-127"}}, True),
        ({"headers": {"range": "bytes=0-127"}}, True),
    ],
)
def test_has_range_header(item, expected):
    assert has_range_header(item) is expected
