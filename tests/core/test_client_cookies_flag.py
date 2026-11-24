"""A client-sent cookie jar must reach the service before its __init__ picks an auth path."""

import base64
import zlib

import pytest

from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError, APIErrorCode
from unshackle.core.api.handlers import load_client_cookies

COOKIE_FILE = "\n".join(
    [
        "# Netscape HTTP Cookie File",
        "\t".join([".primevideo.com", "TRUE", "/", "TRUE", "2000000000", "session-id", "123-456"]),
        "",
    ]
)


def transport_blob(text: str) -> str:
    """Encode a cookie file the way remote_service.load_cookies_for_transport does."""
    return base64.b64encode(zlib.compress(text.encode("utf-8"))).decode("ascii")


def test_load_client_cookies_reads_a_transport_blob():
    jar = load_client_cookies(transport_blob(COOKIE_FILE))
    assert jar is not None
    assert [(c.name, c.value) for c in jar] == [("session-id", "123-456")]


def test_load_client_cookies_without_a_blob():
    assert load_client_cookies(None) is None
    assert load_client_cookies("") is None


@pytest.mark.parametrize("blob", ["not base64!", base64.b64encode(b"not zlib").decode(), transport_blob("garbage")])
def test_load_client_cookies_rejects_a_malformed_blob(blob):
    with pytest.raises(APIError) as exc_info:
        load_client_cookies(blob)
    assert exc_info.value.error_code == APIErrorCode.INVALID_INPUT


@pytest.fixture
def built_ctx(monkeypatch):
    """Stub the service machinery so create_service_instance runs without a real service; return the captured ctx."""
    seen = {}

    def fake_instantiate(parent_ctx, *args):
        seen["ctx"] = parent_ctx
        return object()

    monkeypatch.setattr(handlers, "load_service_yaml", lambda service: {})
    monkeypatch.setattr(handlers, "load_full_cdm", lambda *args: None)
    monkeypatch.setattr(handlers.Services, "load", lambda service: None)
    monkeypatch.setattr(handlers, "instantiate_service", fake_instantiate)

    def build(data):
        _, cookies, _ = handlers.create_service_instance("EXAMPLE", "title", data, None, [], None)
        return seen["ctx"], cookies

    return build


def test_client_jar_sets_the_flag_before_the_service_is_built(built_ctx):
    ctx, cookies = built_ctx({"cookies": transport_blob(COOKIE_FILE)})
    assert ctx.params["cookies_supplied"] is True
    assert cookies is not None and len(cookies) == 1


def test_no_client_jar_leaves_the_flag_false(built_ctx):
    ctx, cookies = built_ctx({})
    assert ctx.params["cookies_supplied"] is False
    assert cookies is None
