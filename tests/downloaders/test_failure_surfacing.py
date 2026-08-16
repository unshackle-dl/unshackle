import socket
import sys
import threading

import pytest
import requests as rq

from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.downloaders.requests import download, has_range_header, parse_content_range, request_range_start
from unshackle.core.downloaders.requests import requests as requests_downloader

# the package re-exports the `requests` function, shadowing the module of the same name
downloader = sys.modules[download.__module__]

BODY = b"".join(f"{i:05d} payload\n".encode() for i in range(500))
CUT = len(BODY) // 2


class _Server:
    """Serves BODY, with a switch for the failure modes retries have to handle."""

    def __init__(self, mode):
        self.mode = mode
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/seg.mp4"
        self.requests = []
        self.plain_hits = 0
        threading.Thread(target=self.serve, daemon=True).start()

    def send_206(self, conn, body, content_range=None):
        head = "HTTP/1.1 206 Partial Content\r\n"
        if content_range:
            head += f"Content-Range: {content_range}\r\n"
        head += f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        conn.sendall(head.encode() + body)

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
                elif not ranged:
                    self.plain_hits += 1
                    if self.mode.startswith("resume_") and self.plain_hits == 1:
                        # promise the full body but hang up early: leaves a CUT-byte
                        # partial behind so the next attempt sends a resume Range
                        conn.sendall(
                            f"HTTP/1.1 200 OK\r\nContent-Length: {len(BODY)}\r\nConnection: close\r\n\r\n".encode()
                            + BODY[:CUT]
                        )
                    else:
                        conn.sendall(
                            f"HTTP/1.1 200 OK\r\nContent-Length: {len(BODY)}\r\nConnection: close\r\n\r\n".encode()
                            + BODY
                        )
                elif self.mode == "resume_whole_body_206":
                    # ignores the requested start: a 206 carrying the entire resource
                    self.send_206(conn, BODY, f"bytes 0-{len(BODY) - 1}/{len(BODY)}")
                elif self.mode == "resume_alien_206":
                    # a 206 starting neither at the requested offset nor at zero
                    self.send_206(conn, BODY[5:], f"bytes 5-{len(BODY) - 1}/{len(BODY)}")
                elif self.mode == "resume_missing_content_range":
                    self.send_206(conn, BODY)
                else:
                    conn.sendall(
                        b"HTTP/1.1 416 Range Not Satisfiable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
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


@pytest.mark.parametrize("server", ["resume_whole_body_206"], indirect=True)
def test_resume_206_carrying_the_whole_body_is_rewritten_not_appended(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    events = list(download(url=server.url, save_path=save_path, session=rq.Session()))
    assert save_path.read_bytes() == BODY
    assert events[-1]["written"] == len(BODY)
    assert [r.get("range") for r in server.requests] == [None, f"bytes={CUT}-"]


@pytest.mark.parametrize("server", ["resume_alien_206"], indirect=True)
def test_resume_206_with_wrong_start_restarts_clean(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    list(download(url=server.url, save_path=save_path, session=rq.Session()))
    assert save_path.read_bytes() == BODY
    assert [r.get("range") for r in server.requests] == [None, f"bytes={CUT}-", None]


@pytest.mark.parametrize("server", ["resume_missing_content_range"], indirect=True)
def test_resume_206_without_content_range_restarts_clean(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    list(download(url=server.url, save_path=save_path, session=rq.Session()))
    assert save_path.read_bytes() == BODY
    assert [r.get("range") for r in server.requests] == [None, f"bytes={CUT}-", None]


@pytest.mark.parametrize("server", ["resume_whole_body_206"], indirect=True)
def test_part_with_wrong_content_range_start_never_writes(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    save_path.write_bytes(b"\x00" * len(BODY))
    with pytest.raises(OSError):
        list(
            download(
                url=server.url,
                save_path=save_path,
                session=rq.Session(),
                part_offset=CUT,
                part_end=len(BODY) - 1,
            )
        )
    assert save_path.read_bytes() == b"\x00" * len(BODY)
    assert len(server.requests) == downloader.MAX_ATTEMPTS


@pytest.mark.parametrize("server", ["resume_whole_body_206"], indirect=True)
def test_slice_segment_rejects_a_206_for_the_wrong_range(tmp_path, server):
    save_path = tmp_path / "0.mp4"
    with pytest.raises(OSError):
        list(
            download(
                url=server.url,
                save_path=save_path,
                session=rq.Session(),
                headers={"Range": f"bytes={CUT}-{len(BODY) - 1}"},
            )
        )
    assert not save_path.exists()
    assert len(server.requests) == downloader.MAX_ATTEMPTS


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


@pytest.mark.parametrize("server", ["ok"], indirect=True)
def test_batch_larger_than_the_queue_window_downloads_every_segment(tmp_path, server):
    # a broken top-up livelocks the drain loop instead of erroring, so consume on a thread and
    # let the timeout fail the test
    urls = [server.url] * 6
    advanced = 0

    def consume():
        nonlocal advanced
        advanced = sum(
            event["advance"]
            for event in requests_downloader(urls=urls, output_dir=tmp_path, filename="{i:02}.mp4", max_workers=1)
            if "advance" in event
        )

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    consumer.join(timeout=30)
    assert not consumer.is_alive(), "downloader never finished the batch"
    assert advanced == len(urls)
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".mp4") == [f"{i:02}.mp4" for i in range(6)]


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("garbage", None),
        ("bytes */400000", None),
        ("bytes 0-399999/400000", (0, 399999, 400000)),
        ("BYTES 150000-399999/*", (150000, 399999, None)),
        (b"bytes 5-9/10", (5, 9, 10)),
    ],
)
def test_parse_content_range(value, expected):
    assert parse_content_range(value) == expected


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, None),
        ({"Accept": "*/*"}, None),
        ({"Range": "bytes=-500"}, None),
        ({"Range": "bytes=100-"}, 100),
        ({"range": "bytes=100-227"}, 100),
    ],
)
def test_request_range_start(headers, expected):
    assert request_range_start(headers) == expected
