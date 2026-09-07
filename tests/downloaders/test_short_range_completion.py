"""Completeness of a sequential download that the server serves in short pieces.

``download()`` renames its ``.!dev`` partial into place and reports ``file_downloaded``
only when the resource is whole. A host that caps every range answers an open-ended
resume (``Range: bytes=N-``) with an honest short 206: the body matches the response's
own ``Content-Length``, so the short-read check is satisfied while the file is still
truncated. These tests pin the completeness decision on the three paths that reach it:

- a resume with a known complete length must continue from the served end until the
  complete length is on disk;
- a resume with an unknown complete length (``Content-Range: bytes N-M/*``) has no
  number to compare against, so the only proof of the end is the server refusing the
  next range with a 416;
- a first request that carries no Range at all, which a host may still answer with a
  short 206. Its Content-Range names the complete length, so the decision has a number
  to compare against even though nothing was resumed.

The guards beside them pin what the decision must not change: an unknown-length body
with no partial on disk still finalizes, a stale oversized partial is still discarded
on a 416, a resume that arrives whole costs no extra request, and an empty resume body
still raises instead of repeating the same request forever.
"""

import importlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from requests import Session

from unshackle.core.constants import DOWNLOAD_CANCELLED

# the package re-exports the `requests` function, shadowing the module of the same name
dl = importlib.import_module("unshackle.core.downloaders.requests")

TOTAL = 256 * 1024
PAYLOAD = bytes((i * 37 + 5) % 256 for i in range(TOTAL))

SEED = 64 * 1024  # bytes a prior run left in the .!dev partial
CAP = 32 * 1024  # most the capped server serves for one range
OPEN_CAP = 16 * 1024  # most the halving server serves for an open-ended range
CUT = 96 * 1024  # where the halving server cuts the unranged body


class _ShortServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list = []
        self.mode = "cap"
        super().__init__(("127.0.0.1", 0), _ShortHandler)

    def handle_error(self, request, client_address):  # the client hanging up is expected here
        pass


class _ShortHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        srv = self.server
        header = self.headers.get("Range")
        with srv.lock:
            srv.requests.append(header)
        mode = srv.mode

        if header is None:
            if mode == "fresh206":
                # a 206 to an un-Ranged request: the complete length is only in Content-Range
                body = PAYLOAD[:CAP]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes 0-{CAP - 1}/{TOTAL}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if mode == "chunked200":
                # no Content-Length: an unknown-length body the downloader cannot size
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"%x\r\n" % len(PAYLOAD) + PAYLOAD + b"\r\n0\r\n\r\n")
                return
            self.send_response(200)
            self.send_header("Content-Length", str(TOTAL))
            self.end_headers()
            if mode == "halving":
                # honest length, then cut: download() credits a resume and bounds the next request
                self.wfile.write(PAYLOAD[:CUT])
                self.close_connection = True
                return
            self.wfile.write(PAYLOAD)
            return

        start_s, _, end_s = header.removeprefix("bytes=").partition("-")
        start = int(start_s)
        want_end = int(end_s) if end_s else None

        if start >= TOTAL:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{TOTAL}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if mode == "empty206":
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{TOTAL - 1}/{TOTAL}")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"0\r\n\r\n")
            return

        if want_end is not None:
            end = min(TOTAL - 1, want_end)  # a bounded sub-range is always served whole
        elif mode == "whole_remainder":
            end = TOTAL - 1
        elif mode == "halving":
            end = min(TOTAL - 1, start + OPEN_CAP - 1)
        else:
            end = min(TOTAL - 1, start + CAP - 1)

        body = PAYLOAD[start : end + 1]
        total = "*" if mode == "cap_star" else str(TOTAL)
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server(monkeypatch):
    DOWNLOAD_CANCELLED.clear()
    monkeypatch.setattr(dl, "RETRY_WAIT", 0)
    srv = _ShortServer()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)
    srv.server_close()
    DOWNLOAD_CANCELLED.clear()


def url(srv):
    return f"http://127.0.0.1:{srv.server_address[1]}/seg.mp4"


def run(srv, save_path):
    return list(dl.download(url=url(srv), save_path=save_path, session=Session(), segmented=True))


def assert_complete(save_path):
    # size first: a truncation failure then names the byte counts instead of diffing blobs
    assert save_path.stat().st_size == TOTAL
    assert save_path.read_bytes() == PAYLOAD


def finalized(events):
    return [event["written"] for event in events if "file_downloaded" in event]


def seed_partial(save_path, size):
    tmp_file = save_path.with_name(f"{save_path.name}.!dev")
    tmp_file.write_bytes(PAYLOAD[:size])
    return tmp_file


def test_capped_open_ended_resume_finishes_the_file(server, tmp_path):
    server.mode = "cap"
    save_path = tmp_path / "seg.mp4"
    tmp_file = seed_partial(save_path, SEED)

    events = run(server, save_path)

    assert_complete(save_path)
    assert finalized(events) == [TOTAL]
    assert not tmp_file.exists()


def test_short_tail_after_chunk_halving_finishes_the_file(server, tmp_path, monkeypatch):
    server.mode = "halving"
    monkeypatch.setattr(dl, "REQUEST_CHUNK_SIZE", 64 * 1024)
    monkeypatch.setattr(dl, "MIN_REQUEST_CHUNK", 8 * 1024)
    monkeypatch.setattr(dl, "MIN_RESUME_PROGRESS", 1024)
    save_path = tmp_path / "seg.mp4"

    events = run(server, save_path)

    assert_complete(save_path)
    assert finalized(events) == [TOTAL]
    # the bounded sub-ranges in the middle were each served whole and continued
    bounded = [r for r in server.requests if r and r.split("-")[-1]]
    assert len(bounded) >= 2


def test_zero_byte_resume_body_raises_instead_of_spinning(server, tmp_path):
    server.mode = "empty206"
    save_path = tmp_path / "seg.mp4"
    seed_partial(save_path, SEED)

    with pytest.raises(IOError):
        run(server, save_path)

    assert not save_path.exists()
    # an empty body must spend an attempt, not repeat the same request with no bound
    assert len(server.requests) <= dl.MAX_ATTEMPTS


def test_unknown_total_resume_finalizes_on_416(server, tmp_path):
    server.mode = "cap_star"
    save_path = tmp_path / "seg.mp4"
    tmp_file = seed_partial(save_path, SEED)

    events = run(server, save_path)

    assert_complete(save_path)
    assert finalized(events) == [TOTAL]
    assert not tmp_file.exists()


def test_stale_partial_416_still_restarts_clean(server, tmp_path):
    server.mode = "cap"
    save_path = tmp_path / "seg.mp4"
    tmp_file = save_path.with_name(f"{save_path.name}.!dev")
    tmp_file.write_bytes(b"\x00" * (TOTAL + 4096))

    events = run(server, save_path)

    assert_complete(save_path)
    assert finalized(events) == [TOTAL]
    assert None in server.requests  # restarted from byte 0 with an unranged GET
    assert not tmp_file.exists()


def test_unknown_length_body_still_finalizes(server, tmp_path):
    server.mode = "chunked200"
    save_path = tmp_path / "seg.mp4"

    events = run(server, save_path)

    assert_complete(save_path)
    assert finalized(events) == [TOTAL]


def test_complete_resume_finalizes_without_an_extra_request(server, tmp_path):
    server.mode = "whole_remainder"
    save_path = tmp_path / "seg.mp4"
    seed_partial(save_path, SEED)

    events = run(server, save_path)

    assert_complete(save_path)
    assert finalized(events) == [TOTAL]
    # a known complete length that the first response satisfies costs no confirming request
    assert len(server.requests) == 1


def test_short_206_to_an_unranged_first_request_finishes_the_file(server, tmp_path):
    """No resume, so the only complete length is the one the 206's Content-Range names."""
    server.mode = "fresh206"
    save_path = tmp_path / "seg.mp4"

    events = run(server, save_path)

    assert_complete(save_path)
    assert finalized(events) == [TOTAL]
