"""Regression tests for downloader resume/completeness robustness.

Each test drives the real ``download()`` generator against a local HTTP server
that injects a specific fault the fault-injection benchmark surfaced:

- truncated segment (clean FIN mid-body) must not finalize as complete
- an interrupted download must resume from the bytes actually written, not a
  pre-allocated full size
- an oversized/stale resume that provokes a 416 must restart clean
- byte-range-slice segments (DASH SegmentBase / HLS EXT-X-BYTERANGE style) must
  never be range-probed or tail-boosted: that would clobber their Range header
  and fetch the whole resource into the segment file

All localhost + small payloads, so they run in the default (not live) suite.
"""

from __future__ import annotations

import importlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import pytest
from requests import Session

# the downloaders package re-exports a `requests` function that shadows this submodule
# attribute, so fetch the real module object from sys.modules via importlib
dl = importlib.import_module("unshackle.core.downloaders.requests")


def _consume(gen) -> list:
    return list(gen)


class _FaultServer:
    """Serves a fixed body with per-request fault injection and Range support."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.attempts = 0  # GETs seen for the single resource
        self.ranges: list[Optional[str]] = []  # Range header per GET, in order
        self.truncate_first = 0  # send only half the body + close, for the first N attempts
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:  # silence
                pass

            def do_GET(self) -> None:  # noqa: N802 (stdlib name)
                with server._lock:
                    server.attempts += 1
                    attempt = server.attempts
                    rng = self.headers.get("Range")
                    server.ranges.append(rng)

                total = len(server.body)
                start = 0
                if rng and rng.startswith("bytes="):
                    start = int(rng.removeprefix("bytes=").split("-", 1)[0])

                if start >= total:  # unsatisfiable: real CDNs answer 416
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.end_headers()
                    return

                chunk = server.body[start:]
                partial = attempt <= server.truncate_first
                declared = len(chunk)  # honest Content-Length...
                sent = chunk[: declared // 2] if partial else chunk  # ...but under-deliver the body

                if rng:
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
                else:
                    self.send_response(200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(declared))
                self.end_headers()
                self.wfile.write(sent)  # short write + return => connection closes (clean FIN)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_FaultServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/file"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dl, "RETRY_WAIT", 0)


def test_truncated_segment_is_not_finalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A segment cut short mid-body must raise, never finalize a truncated file."""
    monkeypatch.setattr(dl, "MAX_ATTEMPTS", 2)
    body = b"A" * 2000
    save = tmp_path / "seg.bin"
    with _FaultServer(body) as srv:
        srv.truncate_first = 99  # every attempt under-delivers
        with pytest.raises(Exception):
            _consume(dl.download(url=srv.url, save_path=save, session=Session(), segmented=True))
    assert not save.exists(), "truncated segment must not be finalized as complete"


def test_interrupted_download_resumes_from_bytes_written(tmp_path: Path) -> None:
    """After a mid-body interruption the retry must Range-resume from the real
    byte count (not a pre-allocated full size) and produce the correct file."""
    body = b"".join(bytes([i % 256]) for i in range(4000))
    save = tmp_path / "file.bin"
    with _FaultServer(body) as srv:
        srv.truncate_first = 1  # first attempt delivers half then closes; retry completes
        _consume(dl.download(url=srv.url, save_path=save, session=Session()))
    assert save.read_bytes() == body
    # attempt 1 had no Range; attempt 2 must resume partway in, not from 0 or past the end
    assert srv.ranges[0] is None
    resume_start = int(srv.ranges[1].removeprefix("bytes=").split("-", 1)[0])
    assert 0 < resume_start < len(body), f"resume offset {resume_start} not derived from bytes written"


def test_resumes_from_preexisting_dev_partial(tmp_path: Path) -> None:
    """A .!dev left on disk by a killed process (Ctrl+C, outage, crash) must resume on the
    next run via a Range request from the partial's size, not restart from zero."""
    body = b"".join(bytes([i % 256]) for i in range(4000))
    save = tmp_path / "file.bin"
    tmp = save.with_name("file.bin.!dev")
    tmp.write_bytes(body[:1500])  # correct prefix the interrupted run flushed to disk
    with _FaultServer(body) as srv:
        _consume(dl.download(url=srv.url, save_path=save, session=Session()))
    assert save.read_bytes() == body
    assert not tmp.exists()
    assert srv.ranges[0] == "bytes=1500-", f"expected resume from 1500, got {srv.ranges[0]}"


def test_byte_range_slice_segments_never_probed_or_boosted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adaptive tail boost must skip segments whose per-item headers carry a Range.

    Such segments are byte-range slices of one larger resource; the boost probe and
    part-mode download would overwrite that Range and pull the entire resource into
    the segment file. A server-side barrier clusters the first six completions so the
    main loop reliably sees spare workers while the two tail slices are still queued,
    the exact window where an unguarded boost would corrupt output.
    """
    seg = 2 * 1024 * 1024
    nseg = 8
    body = b"".join(bytes([i]) * seg for i in range(nseg))  # 16 MiB, distinct byte per slice

    barrier_count = [0]
    barrier_lock = threading.Lock()
    barrier = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802 (stdlib name)
            rng = self.headers.get("Range") or ""
            with barrier_lock:
                barrier_count[0] += 1
                if barrier_count[0] >= 6:
                    barrier.set()
            barrier.wait(5)  # cluster the first six completions; auto-release as a safety net
            start, _, end_s = rng.removeprefix("bytes=").partition("-")
            start = int(start or 0)
            end = int(end_s) if end_s else len(body) - 1
            chunk = body[start : end + 1]
            self.send_response(206 if rng else 200)
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address

    probed: list[str] = []
    real_probe = dl._probe_ranged
    monkeypatch.setattr(
        dl, "_probe_ranged", lambda url, session, **kw: probed.append(url) or real_probe(url, session, **kw)
    )

    url = f"http://{host}:{port}/media.mp4"
    urls = [{"url": url, "headers": {"Range": f"bytes={i * seg}-{(i + 1) * seg - 1}"}} for i in range(nseg)]
    try:
        _consume(
            dl.requests(
                urls=urls,
                output_dir=tmp_path,
                filename="seg_{i:02}{ext}",
                session=Session(),
                max_workers=16,
                adaptive=True,
            )
        )
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert probed == [], "byte-range slice segments must never be range-probed"
    for i in range(nseg):
        data = (tmp_path / f"seg_{i:02}.mp4").read_bytes()
        assert data == body[i * seg : (i + 1) * seg], f"segment {i} corrupted: got {len(data)} bytes"


def test_oversized_resume_416_restarts_clean(tmp_path: Path) -> None:
    """A stale .!dev larger than the resource provokes a 416; the downloader must
    discard it and re-download cleanly rather than burn retries or corrupt."""
    body = b"Z" * 1500
    save = tmp_path / "file.bin"
    tmp = save.with_name(f"{save.name}.!dev")
    tmp.write_bytes(b"X" * 4096)  # poisoned partial, bigger than the server's body
    with _FaultServer(body) as srv:
        _consume(dl.download(url=srv.url, save_path=save, session=Session()))
    assert save.read_bytes() == body
    assert srv.ranges[0] is not None and "4096" in srv.ranges[0]  # first request over-ranged → 416
