"""File I/O rules for HLS segments.

Two faults that Linux never shows:

* The FFmpeg concat list is a UTF-8 file by specification. Written in the OS locale
  encoding it either raises ``UnicodeEncodeError`` on a non-ASCII path, which kills the
  track after every segment is already on disk, or it silently disables the fast path.
* A subtitle segment that the UTF-8 transform leaves unchanged, the common case, needs
  no write back. Each rewrite is a file operation an on-access virus scanner has to
  clear.
"""

from __future__ import annotations

import codecs
import subprocess
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from unshackle.core.manifests import HLS
from unshackle.core.tracks.track import DownloadContext

SEGMENT_ONE = b"WEBVTT\n\n00:00:00.000 --> 00:00:04.000\nhello\n"
SEGMENT_TWO = b"WEBVTT\n\n00:00:04.000 --> 00:00:08.000\nworld\n"


def test_concat_list_is_written_as_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-ASCII segment path must not depend on the OS codepage."""
    save_dir = tmp_path / "Ünshackle тест" / "Subtitle_segments"
    save_dir.mkdir(parents=True)
    segments = []
    for i in range(2):
        segment = save_dir / f"{i:05}.vtt"
        segment.write_bytes(b"\x00" * 8)
        segments.append(segment)

    encodings: list[object] = []
    real_write_text = Path.write_text

    def spy_write_text(self, data, encoding=None, *args, **kwargs):
        encodings.append(encoding)
        return real_write_text(self, data, encoding=encoding, *args, **kwargs)

    seen: list[bytes] = []

    def fake_run(args, **kwargs):
        seen.append(Path(args[args.index("-i") + 1]).read_bytes())
        raise subprocess.CalledProcessError(1, args, output="", stderr="")

    monkeypatch.setattr(Path, "write_text", spy_write_text)
    monkeypatch.setattr("unshackle.core.manifests.hls.binaries.FFMPEG", Path("ffmpeg"))
    monkeypatch.setattr("unshackle.core.manifests.hls.subprocess.run", fake_run)

    HLS.merge_segments(segments=segments, save_path=save_dir.parent / "Subtitle.vtt")

    assert encodings, "the concat list was never written"
    assert all(e is not None and codecs.lookup(e).name == "utf-8" for e in encodings), (
        f"the concat list was written in the OS locale encoding: {encodings}"
    )
    assert seen and seen[0].decode("utf-8") == "\n".join(f"file '{s.absolute()}'" for s in segments)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.routes.get(self.path)
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
    srv.routes = {}
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",DEFAULT=YES,URI="subs.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1920x1080,CODECS="avc1.640028",SUBTITLES="subs"
media.m3u8
"""

SUBS_PLAYLIST = b"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
part0.vtt
#EXTINF:4.0,
part1.vtt
#EXT-X-ENDLIST
"""


def test_unchanged_subtitle_segments_are_not_rewritten(server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain UTF-8 WebVTT survives the transform untouched, so nothing needs writing back."""
    port = server.server_address[1]
    server.routes["/subs.m3u8"] = SUBS_PLAYLIST
    server.routes["/part0.vtt"] = SEGMENT_ONE
    server.routes["/part1.vtt"] = SEGMENT_TWO

    track = HLS.from_text(MASTER, url=f"http://127.0.0.1:{port}/master.m3u8").to_tracks(language="en").subtitles[0]

    save_dir = tmp_path / "s0_segments"
    rewritten: list[str] = []
    real_write_bytes = Path.write_bytes

    def spy_write_bytes(self, data):
        if save_dir in self.parents:
            rewritten.append(self.name)
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", spy_write_bytes)

    HLS.download_track(
        track,
        DownloadContext(
            save_path=tmp_path / "out.vtt",
            save_dir=save_dir,
            progress=partial(lambda **kw: None),
            max_workers=1,
        ),
    )

    assert (tmp_path / "out.vtt").read_bytes() == SEGMENT_ONE + SEGMENT_TWO
    assert rewritten == [], f"unchanged subtitle segments were rewritten: {rewritten}"
