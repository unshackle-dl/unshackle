"""URL-backed attachments defer their fetch to download(), which honors no_proxy_download."""

from __future__ import annotations

import importlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

import unshackle.core.tracks.track as track_module
from unshackle.commands.dl import dl
from unshackle.core.config import config
from unshackle.core.tracks.attachment import Attachment

downloader = importlib.import_module("unshackle.core.downloaders.requests")
BODY = bytes(range(256)) * 64


class Handler(BaseHTTPRequestHandler):
    """Serves BODY (honoring a Range start), or the next scripted status or "cut" first."""

    def do_GET(self) -> None:
        self.server.hits += 1  # type: ignore[attr-defined]
        script = self.server.script  # type: ignore[attr-defined]
        action = script.pop(0) if script else "ok"
        if isinstance(action, int):
            self.send_response(action)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rng = self.headers.get("Range")
        start = int(rng.removeprefix("bytes=").split("-")[0]) if rng else 0
        body = BODY[start:]
        self.send_response(206 if rng else 200)
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{len(BODY) - 1}/{len(BODY)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body[: len(body) // 2] if action == "cut" else body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.hits = 0  # type: ignore[attr-defined]
    srv.script = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def temp_and_no_wait(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config.directories, "temp", tmp_path)
    monkeypatch.setattr(downloader, "retry_sleep", lambda exc, attempts: 0)


def url(srv, path: str = "/images/thumb.jpg") -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


class Recorder(requests.Session):
    __attrs__ = [*requests.Session.__attrs__, "got"]

    def __init__(self, proxies: dict | None = None) -> None:
        super().__init__()
        self.proxies = proxies or {}
        self.got: list[str] = []

    def get(self, url, **kwargs):  # type: ignore[override]
        self.got.append(url)
        return super().get(url, **kwargs)


def test_url_attachment_defers_download() -> None:
    session = Recorder()
    attachment = Attachment.from_url("https://cdn.example.com/images/thumb.jpg", name="thumbnail", session=session)
    assert attachment.path is None
    assert not session.got
    assert attachment.mime_type == "image/jpeg"


def test_download_without_flag_uses_proxied_session(server) -> None:
    session = Recorder(proxies={"https": "http://proxy:8080"})
    attachment = Attachment.from_url(url(server), name="thumbnail", session=session)
    attachment.download(session)
    assert session.got == [url(server)]
    assert attachment.path is not None and attachment.path.read_bytes() == BODY


def test_download_with_flag_bypasses_proxy(server, monkeypatch) -> None:
    direct = Recorder()
    monkeypatch.setattr(track_module, "direct_session", lambda session: direct)
    proxied = Recorder(proxies={"https": "http://proxy:8080"})
    attachment = Attachment.from_url(url(server), name="thumbnail", session=proxied)
    attachment.download(proxied, no_proxy_download=True)
    assert not proxied.got
    assert direct.got == [url(server)]
    assert attachment.path is not None and attachment.path.read_bytes() == BODY


def test_download_with_proxy_download_uses_only_that_proxy(server, monkeypatch) -> None:
    seen: dict = {}

    def fake_direct(session, proxy=None):
        seen["proxy"] = proxy
        return Recorder()

    monkeypatch.setattr(track_module, "direct_session", fake_direct)
    proxied = Recorder(proxies={"https": "http://manifest-proxy:1"})
    attachment = Attachment.from_url(url(server), name="thumbnail", session=proxied)
    attachment.download(proxied, proxy_download="http://dl-proxy:2")
    assert not proxied.got
    assert seen["proxy"] == "http://dl-proxy:2"
    assert proxied.proxies == {"https": "http://manifest-proxy:1"}


def test_no_proxy_download_wins_over_proxy_download(server, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(track_module, "direct_session", lambda s, proxy=None: seen.setdefault("p", proxy) or Recorder())
    plain = Recorder()
    Attachment.from_url(url(server), name="t", session=plain).download(
        plain, no_proxy_download=True, proxy_download="http://dl-proxy:2"
    )
    assert "p" not in seen and plain.got == [url(server)]


def test_body_cut_is_resumed(server) -> None:
    server.script = ["cut"]
    attachment = Attachment.from_url(url(server), name="thumbnail")
    attachment.download(requests.Session())
    assert server.hits == 2
    assert attachment.path is not None and attachment.path.read_bytes() == BODY


def test_spent_retries_fail_the_run_with_one_line(server) -> None:
    server.script = [503] * downloader.MAX_ATTEMPTS
    attachment = Attachment.from_url(url(server), name="thumbnail")
    with pytest.raises(requests.HTTPError) as raised:
        attachment.download(requests.Session())
    assert server.hits == downloader.MAX_ATTEMPTS
    assert attachment.path is None
    _, line = dl.failure_lines(raised.value)
    assert f"gave up after {downloader.MAX_ATTEMPTS} failed attempts" in line
    assert "attachment thumbnail" in line


def test_same_named_attachments_get_separate_files(server) -> None:
    first = Attachment.from_url(url(server, "/a/image.jpg"))
    second = Attachment.from_url(url(server, "/b/image.jpg"))
    first.download(requests.Session())
    second.download(requests.Session())
    assert first.path is not None and second.path is not None
    assert first.path != second.path


def test_direct_session_routes_through_the_given_proxy() -> None:
    base = requests.Session()
    base.headers["X-Test"] = "1"
    new = track_module.direct_session(base, "http://dl-proxy:2")
    assert new.headers["X-Test"] == "1"
    assert new.proxies == {"http": "http://dl-proxy:2", "https": "http://dl-proxy:2"}
    assert requests.utils.select_proxy("https://cdn.example.com/a", new.proxies) == "http://dl-proxy:2"


def test_direct_session_proxy_beats_environment(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy:9")
    new = track_module.direct_session(requests.Session(), "http://dl-proxy:2")
    settings = new.merge_environment_settings("https://cdn.example.com/a", {}, None, None, None)
    assert settings["proxies"]["https"] == "http://dl-proxy:2"


def test_direct_session_copies_rnet_cookies_set_by_name() -> None:
    from unshackle.core.session import RnetSession

    rs = RnetSession()
    rs.cookies.set("via_set", "1", domain="cdn.example.com")
    rs.cookies["via_item"] = "3"
    new = track_module.direct_session(rs, "http://dl-proxy:2")
    assert new.cookies.get("via_set") == "1"
    assert new.cookies.get("via_item") == "3"


def test_delete_removes_only_files_in_the_temp_directory(tmp_path, monkeypatch) -> None:
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(config.directories, "temp", temp)
    temp_font = temp / "font.ttf"
    user_font = tmp_path / "font.ttf"
    temp_font.write_bytes(b"font")
    user_font.write_bytes(b"font")

    for path in (temp_font, user_font):
        attachment = Attachment(path=path)
        attachment.delete()
        assert attachment.path is None

    assert not temp_font.exists()
    assert user_font.exists()
