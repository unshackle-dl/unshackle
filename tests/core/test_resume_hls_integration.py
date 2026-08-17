"""Integration coverage for cross-run segment resume on the HLS path.

Same two-run shape as the DASH test, plus the HLS-specific rules: the sidecar (the
"download phase completed cleanly" proof) survives a mid-download crash and is gone once
the merge pass runs, and AES-128 content is excluded from resume entirely because its
segments are decrypted in place during the merge pass.
"""

from __future__ import annotations

import importlib
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.manifests import HLS
from unshackle.core.tracks import resume
from unshackle.core.tracks.track import DownloadContext

PART0 = bytes([0xA0]) * 300
PART1 = bytes([0xB1]) * 200
AES_KEY = bytes(range(16))


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        with server.lock:
            server.requests.append(self.path)
        body = server.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.lock = threading.Lock()
    srv.requests = []
    srv.routes = {}
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def resume_enabled(monkeypatch):
    monkeypatch.setattr(config, "continue_downloads", True)
    monkeypatch.setattr(importlib.import_module("unshackle.core.downloaders.requests"), "MAX_ATTEMPTS", 1)
    DOWNLOAD_CANCELLED.clear()
    yield
    DOWNLOAD_CANCELLED.clear()


def base(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/"


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1920x1080,CODECS="avc1.640028"
media.m3u8
"""


def media_playlist(key_line: str = "") -> bytes:
    return f"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
{key_line}#EXTINF:4.0,
part0.bin
#EXTINF:4.0,
part1.bin
#EXT-X-ENDLIST
""".encode()


def make_track(srv):
    return HLS.from_text(MASTER, url=f"{base(srv)}master.m3u8").to_tracks(language="en").videos[0]


def make_ctx(tmp_path):
    return DownloadContext(
        save_path=tmp_path / "out.mp4",
        save_dir=tmp_path / "v0_segments",
        progress=partial(lambda **kw: None),
        max_workers=1,
    )


def test_hls_second_run_reuses_completed_segments(server, tmp_path):
    server.routes["/media.m3u8"] = media_playlist()
    server.routes["/part0.bin"] = PART0
    # part1.bin missing -> run 1 completes part0 then dies during the download phase

    ctx = make_ctx(tmp_path)
    with pytest.raises(Exception):
        HLS.download_track(make_track(server), ctx)

    assert (ctx.save_dir / "segments" / "0.bin").read_bytes() == PART0
    assert resume.sidecar_path(ctx.save_dir).exists()
    part0_fetches_run1 = server.requests.count("/part0.bin")

    DOWNLOAD_CANCELLED.clear()
    server.routes["/part1.bin"] = PART1
    HLS.download_track(make_track(server), ctx)

    assert ctx.save_path.read_bytes() == PART0 + PART1
    assert server.requests.count("/part0.bin") == part0_fetches_run1
    assert not resume.sidecar_path(ctx.save_dir).exists()


def test_hls_aes128_content_never_writes_sidecar(server, tmp_path):
    key_line = f'#EXT-X-KEY:METHOD=AES-128,URI="{base(server)}key.bin"\n'
    server.routes["/media.m3u8"] = media_playlist(key_line)
    server.routes["/key.bin"] = AES_KEY
    server.routes["/part0.bin"] = PART0
    # part1.bin missing -> crash mid-download, the state resume would normally preserve

    ctx = make_ctx(tmp_path)
    with pytest.raises(Exception):
        HLS.download_track(make_track(server), ctx)

    assert not resume.sidecar_path(ctx.save_dir).exists()
