"""URL-backed attachments defer their fetch to download(), which honors no_proxy_download."""

from __future__ import annotations

import requests

import unshackle.core.tracks.track as track_module
from unshackle.core.config import config
from unshackle.core.tracks.attachment import Attachment

URL = "https://cdn.example.com/images/thumb.jpg"


class FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int):
        yield b"jpegdata"


class FakeSession(requests.Session):
    def __init__(self, proxies: dict | None = None) -> None:
        super().__init__()
        self.proxies = proxies or {}
        self.got: list[str] = []

    def get(self, url, **kwargs):  # type: ignore[override]
        self.got.append(url)
        return FakeResponse()


def test_url_attachment_defers_download() -> None:
    session = FakeSession()
    attachment = Attachment.from_url(URL, name="thumbnail", session=session)
    assert attachment.path is None
    assert not session.got
    assert attachment.mime_type == "image/jpeg"


def test_download_without_flag_uses_proxied_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config.directories, "temp", tmp_path)
    session = FakeSession(proxies={"https": "http://proxy:8080"})
    attachment = Attachment.from_url(URL, name="thumbnail", session=session)
    attachment.download(session)
    assert session.got == [URL]
    assert attachment.path is not None and attachment.path.read_bytes() == b"jpegdata"


def test_download_with_flag_bypasses_proxy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config.directories, "temp", tmp_path)
    direct = FakeSession()
    monkeypatch.setattr(track_module, "direct_session", lambda session: direct)
    proxied = FakeSession(proxies={"https": "http://proxy:8080"})
    attachment = Attachment.from_url(URL, name="thumbnail", session=proxied)
    attachment.download(proxied, no_proxy_download=True)
    assert not proxied.got
    assert direct.got == [URL]
    assert attachment.path is not None and attachment.path.read_bytes() == b"jpegdata"


def test_download_with_proxy_download_uses_only_that_proxy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config.directories, "temp", tmp_path)
    seen: dict = {}

    def fake_direct(session, proxy=None):
        seen["proxy"] = proxy
        return FakeSession(proxies={"all": proxy})

    monkeypatch.setattr(track_module, "direct_session", fake_direct)
    proxied = FakeSession(proxies={"https": "http://manifest-proxy:1"})
    attachment = Attachment.from_url(URL, name="thumbnail", session=proxied)
    attachment.download(proxied, proxy_download="http://dl-proxy:2")
    assert not proxied.got
    assert seen["proxy"] == "http://dl-proxy:2"
    assert proxied.proxies == {"https": "http://manifest-proxy:1"}


def test_no_proxy_download_wins_over_proxy_download(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config.directories, "temp", tmp_path)
    seen: dict = {}
    monkeypatch.setattr(
        track_module, "direct_session", lambda s, proxy=None: seen.setdefault("p", proxy) or FakeSession()
    )
    plain = FakeSession()
    Attachment.from_url(URL, name="t", session=plain).download(
        plain, no_proxy_download=True, proxy_download="http://dl-proxy:2"
    )
    assert "p" not in seen and plain.got == [URL]


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
