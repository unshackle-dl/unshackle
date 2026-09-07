"""A sequential download must not finalize a body it cannot prove is whole, and must not
retry one for free.

Every shape here reaches ``os.replace(tmp_file, save_path)`` or a ``continue`` that skips
the retry handler. The first family renames a short file into place and reports it as a
finished track, so the merge takes a truncated segment; the second repeats one request
forever, spending no attempt and taking no backoff.

Each test runs the download in a daemon thread with a hard join deadline, so a regression
fails the suite instead of stalling it.
"""

import gzip
import importlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from requests import Session

from unshackle.core.constants import DOWNLOAD_CANCELLED

dl = importlib.import_module("unshackle.core.downloaders.requests")

TOTAL = 8192
PAYLOAD = bytes((i * 37 + 11) % 256 for i in range(TOTAL))
PREFIX = 1024  # what a capping or lying host serves instead of the whole thing
SLICE_END = 4095  # inclusive end of the byte range the slice tests ask for
DEADLINE = 60


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list = []
        self.mode = ""
        super().__init__(("127.0.0.1", 0), _Handler)

    def handle_error(self, request, client_address):  # the client hanging up is expected here
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def chunked(self, status: int, body: bytes, **headers: str) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if body:
            self.wfile.write(b"%x\r\n" % len(body) + body + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def sized(self, status: int, body: bytes, **headers: str) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        srv = self.server
        header = self.headers.get("Range")
        with srv.lock:
            srv.requests.append(header)
        mode = srv.mode
        start = int(header.removeprefix("bytes=").split("-")[0]) if header else 0

        if mode == "empty200":
            return self.chunked(200, b"")
        if mode == "chunked200":
            return self.chunked(200, PAYLOAD)
        if mode in ("short_slice", "good_slice"):
            end = int(header.removeprefix("bytes=").split("-")[1])
            body = PAYLOAD[start : end + 1]
            if mode == "short_slice":
                body = body[:PREFIX]
            return self.chunked(206, body, Content_Range=f"bytes {start}-{end}/{TOTAL}")
        if mode == "star206":
            if start >= TOTAL:
                return self.sized(416, b"", Content_Range=f"bytes */{TOTAL}")
            end = min(TOTAL - 1, start + PREFIX - 1)
            return self.sized(206, PAYLOAD[start : end + 1], Content_Range=f"bytes {start}-{end}/*")
        if mode == "nonzero_start206":
            half = TOTAL // 2
            return self.sized(206, PAYLOAD[half:], Content_Range=f"bytes {half}-{TOTAL - 1}/{TOTAL}")
        if mode == "prefix206":
            return self.sized(206, PAYLOAD[:PREFIX], Content_Range=f"bytes 0-{PREFIX - 1}/{TOTAL}")
        if mode == "encoded_resume":
            return self.sized(
                206,
                gzip.compress(PAYLOAD[start : start + PREFIX]),
                Content_Range=f"bytes {start}-{TOTAL - 1}/{TOTAL}",
                Content_Encoding="gzip",
            )
        # the modes below answer the first, un-Ranged request with a short but honest 206,
        # which teaches the download a complete length to resume towards
        if header is None:
            return self.sized(206, PAYLOAD[:PREFIX], Content_Range=f"bytes 0-{PREFIX - 1}/{TOTAL}")
        if mode == "ignore_range200":
            return self.sized(200, PAYLOAD[:PREFIX])
        if mode == "bare416":
            return self.sized(416, b"")
        if mode == "shorter416":
            return self.sized(416, b"", Content_Range=f"bytes */{PREFIX}")
        raise AssertionError(f"unknown mode {mode!r}")


@pytest.fixture
def server(monkeypatch):
    DOWNLOAD_CANCELLED.clear()
    monkeypatch.setattr(dl, "RETRY_WAIT", 0)
    srv = _Server()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)
    srv.server_close()
    DOWNLOAD_CANCELLED.clear()


def run(srv, save_path, **kwargs):
    """Drain download() on a daemon thread, giving back (events, error)."""
    url = f"http://127.0.0.1:{srv.server_address[1]}/seg.mp4"
    events: list = []
    errors: list = []

    def go() -> None:
        try:
            events.extend(dl.download(url=url, save_path=save_path, session=Session(), **kwargs))
        except BaseException as exc:  # noqa: BLE001 - the deadline assert reports it
            errors.append(exc)

    worker = threading.Thread(target=go, daemon=True)
    worker.start()
    worker.join(timeout=DEADLINE)
    assert not worker.is_alive(), f"download still running after {DEADLINE}s ({len(srv.requests)} requests)"
    return events, (errors[0] if errors else None)


def assert_bounded(srv, error, save_path, per_attempt=1):
    assert isinstance(error, IOError), f"expected an IOError, got {error!r}"
    assert not save_path.exists(), f"finalized {save_path.stat().st_size} bytes"
    assert len(srv.requests) <= dl.MAX_ATTEMPTS * per_attempt + 1


def test_empty_200_body_is_not_a_finished_file(server, tmp_path):
    """A chunked 200 with no body: unknown length is not proof that the resource is empty."""
    server.mode = "empty200"
    save_path = tmp_path / "seg.mp4"

    _, error = run(server, save_path)

    assert_bounded(server, error, save_path)


def test_short_byte_range_slice_is_not_a_finished_segment(server, tmp_path):
    """A slice carries no Content-Length here, so its Content-Range is the only expected size."""
    server.mode = "short_slice"
    save_path = tmp_path / "seg.mp4"

    _, error = run(server, save_path, headers={"Range": f"bytes=0-{SLICE_END}"}, segmented=True)

    assert_bounded(server, error, save_path)


def test_whole_byte_range_slice_still_finalizes(server, tmp_path):
    """The guard above must not reject a chunked slice that arrives whole."""
    server.mode = "good_slice"
    save_path = tmp_path / "seg.mp4"

    _, error = run(server, save_path, headers={"Range": f"bytes=0-{SLICE_END}"}, segmented=True)

    assert error is None
    assert save_path.read_bytes() == PAYLOAD[: SLICE_END + 1]


def test_unknown_total_206_resumes_instead_of_finalizing_short(server, tmp_path):
    """`bytes 0-N/*` names no complete length, so only a 416 can end the download."""
    server.mode = "star206"
    save_path = tmp_path / "seg.mp4"

    _, error = run(server, save_path, segmented=True)

    assert error is None
    assert save_path.read_bytes() == PAYLOAD


def test_206_starting_past_byte_zero_is_refused(server, tmp_path):
    """Nothing repositions the write, so those bytes would land at offset 0."""
    server.mode = "nonzero_start206"
    save_path = tmp_path / "seg.mp4"

    _, error = run(server, save_path)

    assert_bounded(server, error, save_path)


def test_resume_answered_with_the_same_prefix_spends_attempts(server, tmp_path):
    server.mode = "prefix206"
    save_path = tmp_path / "seg.mp4"

    _, error = run(server, save_path)

    assert_bounded(server, error, save_path, per_attempt=2)


def test_resume_ignored_by_a_200_spends_attempts(server, tmp_path):
    """The 200 re-sends the same prefix, so the file never grows past where it started."""
    server.mode = "ignore_range200"
    save_path = tmp_path / "seg.mp4"

    _, error = run(server, save_path)

    assert_bounded(server, error, save_path, per_attempt=2)


def test_content_encoded_resume_spends_attempts(server, tmp_path):
    server.mode = "encoded_resume"
    save_path = tmp_path / "seg.mp4"

    _, error = run(server, save_path)

    assert_bounded(server, error, save_path, per_attempt=2)


def test_bare_416_restart_spends_attempts(server, tmp_path):
    """A 416 with no Content-Range proves nothing about where the resource ends."""
    server.mode = "bare416"
    save_path = tmp_path / "seg.mp4"

    _, error = run(server, save_path)

    assert_bounded(server, error, save_path, per_attempt=2)


def test_416_shorter_than_the_known_total_does_not_finalize(server, tmp_path):
    """Two responses name two complete lengths; the partial matches only the shorter one."""
    server.mode = "shorter416"
    save_path = tmp_path / "seg.mp4"

    _, error = run(server, save_path)

    assert_bounded(server, error, save_path, per_attempt=2)


def test_unknown_length_body_with_real_bytes_still_finalizes(server, tmp_path):
    """A chunked 200 that arrives whole must still finalize."""
    server.mode = "chunked200"
    save_path = tmp_path / "seg.mp4"

    events, error = run(server, save_path, segmented=True)

    assert error is None
    assert save_path.read_bytes() == PAYLOAD
    assert [event["written"] for event in events if "file_downloaded" in event] == [TOTAL]
