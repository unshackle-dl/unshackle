"""Segment requests on rnet carry network.read_timeout from the config, or READ_TIMEOUT when it is unset.

A per-request read_timeout overrides the one on the rnet client, so the downloader must pass the configured value.
"""

import importlib

import pytest

from tests.downloaders.test_content_encoding import _FakeRnetResponse
from unshackle.core.config import config

dl = importlib.import_module("unshackle.core.downloaders.requests")


class FakeSession:
    def __init__(self):
        self.calls: list[dict] = []

    def get(self, url, stream=True, **kwargs):
        self.calls.append(kwargs)
        return _FakeRnetResponse(200, {}, b"body-bytes")


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(dl, "is_rnet_session", lambda s: isinstance(s, FakeSession))
    return FakeSession()


@pytest.mark.parametrize(
    "network, module_default, kwargs, expected",
    [
        pytest.param({"read_timeout": 7}, None, {}, 7, id="configured"),
        pytest.param({}, None, {}, dl.READ_TIMEOUT, id="unset"),
        pytest.param({}, 2, {}, 2, id="patched-default"),
        pytest.param({"read_timeout": 7}, None, {"read_timeout": 3}, 3, id="caller-wins"),
    ],
)
def test_segment_request_read_timeout(tmp_path, session, monkeypatch, network, module_default, kwargs, expected):
    monkeypatch.setattr(config, "network", network)
    if module_default is not None:
        monkeypatch.setattr(dl, "READ_TIMEOUT", module_default)
    list(dl.download(url="http://127.0.0.1:1/a", save_path=tmp_path / "a.bin", session=session, **kwargs))
    assert session.calls[0]["read_timeout"] == expected


def test_probe_uses_the_configured_read_timeout(session, monkeypatch):
    monkeypatch.setattr(config, "network", {"read_timeout": 7})
    dl.probe_ranged("http://127.0.0.1:1/a", session)
    assert session.calls[0]["read_timeout"] == 7
