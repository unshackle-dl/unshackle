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
