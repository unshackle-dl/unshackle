"""Rolling merge through ``HLS.download_track`` and ``ISM.download_track``.

Same localhost-server shape as the DASH collapse test. Each parser downloads one unencrypted
track twice, with ``merge_segments`` off and on, and the two output files must be byte
identical with no segment directory left behind, and the rolling run must report no Merging
phase (the post-download run must). A second HLS case with a discontinuity checks that an
ineligible playlist keeps the post-download merge and still produces the same bytes. A stalled
drain must raise and leave no output file.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

import pytest
from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad
from pywidevine.pssh import PSSH

from tests.core.test_ism_init import piff_fragment
from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.drm import Widevine
from unshackle.core.manifests import HLS, ISM
from unshackle.core.manifests import hls as hls_module
from unshackle.core.manifests import ism as ism_module
from unshackle.core.manifests.dash import RollingMerge
from unshackle.core.manifests.ism_init import PIFF_SENC_UUID
from unshackle.core.tracks.track import DownloadContext

INIT = bytes(range(100))
PARTS = [bytes([0xA0 + i]) * (150 + 37 * i) for i in range(5)]
KID = UUID(hex="11111111111111111111111111111111")
AES_KEY = bytes(range(16))
DECRYPTED_INIT = b"decrypted-init" * 8
# one PIFF fragment per index, each with its own mdat payload so a misordered append shows up
PIFF_PARTS = [piff_fragment().replace(b"\xab" * 64, bytes([0xC0 + i]) * 64) for i in range(len(PARTS))]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.routes.get(self.path.split("?")[0])
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
    srv.routes = {"/init.mp4": INIT}
    for i, part in enumerate(PARTS):
        srv.routes[f"/part{i}.m4s"] = srv.routes[f"/x.ism/part{i}.m4s"] = part
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def no_cancel():
    DOWNLOAD_CANCELLED.clear()
    yield
    DOWNLOAD_CANCELLED.clear()


def base(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/"


def make_ctx(tmp_path, name):
    statuses: list[str] = []

    def progress(**kw):
        if kw.get("downloaded"):
            statuses.append(kw["downloaded"])

    ctx = DownloadContext(
        save_path=tmp_path / name,
        save_dir=tmp_path / f"{name}_segments",
        progress=partial(progress),
        max_workers=4,
    )
    ctx.statuses = statuses
    return ctx


def hls_track(srv, discontinuity: bool):
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:4", '#EXT-X-MAP:URI="init.mp4"']
    for i in range(len(PARTS)):
        if discontinuity and i == 2:
            lines += ["#EXT-X-DISCONTINUITY", '#EXT-X-MAP:URI="init.mp4"']
        lines += ["#EXTINF:4.0,", f"part{i}.m4s"]
    lines.append("#EXT-X-ENDLIST")
    srv.routes["/media.m3u8"] = "\n".join(lines).encode()
    master = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1920x1080,CODECS="avc1.640028"\nmedia.m3u8\n'
    return HLS.from_text(master, url=f"{base(srv)}master.m3u8").to_tracks(language="en").videos[0]


def ism_track(srv):
    # audio: the init synthesis needs only CodecPrivateData, no first-segment parse
    manifest = f"""<?xml version="1.0"?>
<SmoothStreamingMedia MajorVersion="2" MinorVersion="0" Duration="{len(PARTS) * 20000000}" TimeScale="10000000">
  <StreamIndex Type="audio" Name="audio" Language="en" Url="part{{start_time}}.m4s">
    <QualityLevel Index="0" Bitrate="128000" FourCC="AACL" AudioTag="255" SamplingRate="48000"
                  Channels="2" BitsPerSample="16" PacketSize="4" CodecPrivateData="1190"/>
    <c t="0" d="1" r="{len(PARTS)}"/>
  </StreamIndex>
</SmoothStreamingMedia>
"""
    return ISM.from_text(manifest, f"{base(srv)}x.ism/manifest").to_tracks().audio[0]


def download_both_ways(parser, make, tmp_path, monkeypatch, expect_rolling: bool) -> tuple[bytes, bytes]:
    monkeypatch.setattr(config, "merge_segments", False)
    plain = make_ctx(tmp_path, "plain.mp4")
    parser.download_track(make(), plain)
    assert "Merging" in plain.statuses

    monkeypatch.setattr(config, "merge_segments", True)
    rolling = make_ctx(tmp_path, "rolling.mp4")
    parser.download_track(make(), rolling)
    assert not rolling.save_dir.exists() or not list(rolling.save_dir.glob("*"))
    # the rolling path never reports a Merging phase; an ineligible track must still report one
    assert ("Merging" not in rolling.statuses) == expect_rolling
    return plain.save_path.read_bytes(), rolling.save_path.read_bytes()


def test_hls_single_init_rolling_matches_post_merge(server, tmp_path, monkeypatch):
    plain, rolling = download_both_ways(HLS, lambda: hls_track(server, False), tmp_path, monkeypatch, True)
    assert rolling == plain == INIT + b"".join(PARTS)


def test_hls_discontinuity_keeps_post_merge(server, tmp_path, monkeypatch):
    plain, rolling = download_both_ways(HLS, lambda: hls_track(server, True), tmp_path, monkeypatch, False)
    assert rolling == plain
    assert INIT in rolling[len(INIT) :]


def test_ism_rolling_matches_post_merge(server, tmp_path, monkeypatch):
    plain, rolling = download_both_ways(ISM, lambda: ism_track(server), tmp_path, monkeypatch, True)
    assert rolling == plain
    assert rolling.endswith(b"".join(PARTS))
    assert len(rolling) > len(b"".join(PARTS))


@pytest.mark.parametrize("parser, make", [(HLS, hls_track), (ISM, lambda srv, _d=None: ism_track(srv))])
def test_incomplete_rolling_merge_raises_and_leaves_no_output(server, tmp_path, monkeypatch, parser, make):
    monkeypatch.setattr(config, "merge_segments", True)
    # a drain that never reaches the end must not ship a short file as a finished track
    monkeypatch.setattr(RollingMerge, "add", lambda self, *a, **kw: None)
    ctx = make_ctx(tmp_path, "short.mp4")
    with pytest.raises(FileNotFoundError, match="Rolling merge appended 0 of"):
        parser.download_track(make(server, False), ctx)
    assert not ctx.save_path.exists()


class FakeDRM:
    """The attributes the ISM merge reads from a licensed DRM: the KID for the synthesised tenc."""

    kids = [KID]


class FakeSegmentDecrypter:
    """Per-segment decryption that hands each segment straight back, with a fixed decrypted init."""

    def __init__(self, drm, init_content, workdir, max_workers):
        self.submitted: list = []

    def init_bytes(self) -> bytes:
        return DECRYPTED_INIT

    def submit(self, segment) -> Future:
        self.submitted.append(segment)
        future: Future = Future()
        future.set_result(segment)
        return future

    def finish(self) -> bytes:
        return DECRYPTED_INIT

    def close(self) -> None:
        pass


def no_decrypt(monkeypatch, module):
    # the merged file is compared byte for byte, so the decrypt step and its check are stubbed
    monkeypatch.setattr(module, "decrypt_track", lambda *a, **kw: None)
    monkeypatch.setattr(module, "assert_fragments_decrypted", lambda *a, **kw: None)


def test_ism_encrypted_rolling_matches_post_merge(server, tmp_path, monkeypatch):
    for i, part in enumerate(PIFF_PARTS):
        server.routes[f"/x.ism/part{i}.m4s"] = part
    no_decrypt(monkeypatch, ism_module)

    def make():
        track = ism_track(server)
        track.drm = [FakeDRM()]
        return track

    plain, rolling = download_both_ways(ISM, make, tmp_path, monkeypatch, True)
    assert rolling == plain
    # every fragment went through the PIFF to CENC rewrite, and the head carries the KID
    assert PIFF_SENC_UUID not in rolling
    assert rolling.count(b"senc") == len(PIFF_PARTS)
    assert bytes.fromhex(KID.hex) in rolling
    assert rolling.index(b"moov") < rolling.index(b"moof")
    for i in range(len(PIFF_PARTS)):
        assert bytes([0xC0 + i]) * 64 in rolling


def test_hls_segment_decrypt_rolling_writes_decrypted_init(server, tmp_path, monkeypatch):
    monkeypatch.setattr(hls_module, "can_use", lambda *a, **kw: True)
    monkeypatch.setattr(hls_module, "SegmentDecrypter", FakeSegmentDecrypter)
    no_decrypt(monkeypatch, hls_module)

    def make():
        track = hls_track(server, False)
        track.drm = [Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[KID]))]
        return track

    def download(name, merge_segments):
        monkeypatch.setattr(config, "merge_segments", merge_segments)
        ctx = make_ctx(tmp_path, name)
        ctx.license_widevine = lambda *a, **kw: None
        HLS.download_track(make(), ctx)
        return ctx

    plain = download("plain.mp4", False)
    rolling = download("rolling.mp4", True)
    assert "Merging" in plain.statuses and "Merging" not in rolling.statuses
    assert "Decrypting" in rolling.statuses
    assert rolling.save_path.read_bytes() == plain.save_path.read_bytes() == DECRYPTED_INIT + b"".join(PARTS)
    assert not rolling.save_dir.exists() or not list(rolling.save_dir.glob("*"))


def test_hls_aes128_keeps_post_merge(server, tmp_path, monkeypatch):
    # AES-128 with no IV attribute: each segment's IV is its media sequence number
    for i, part in enumerate(PARTS):
        server.routes[f"/part{i}.m4s"] = AES.new(AES_KEY, AES.MODE_CBC, i.to_bytes(16, "big")).encrypt(pad(part, 16))
    server.routes["/key.bin"] = AES_KEY

    def make():
        track = hls_track(server, False)
        playlist = server.routes["/media.m3u8"].decode()
        key_line = f'#EXT-X-KEY:METHOD=AES-128,URI="{base(server)}key.bin"\n'
        server.routes["/media.m3u8"] = playlist.replace("#EXTINF", key_line + "#EXTINF", 1).encode()
        return track

    plain, rolling = download_both_ways(HLS, make, tmp_path, monkeypatch, False)
    assert rolling == plain == INIT + b"".join(PARTS)
