import logging
from pathlib import Path
from uuid import UUID

import click
import pytest
import requests

from unshackle.commands.kv import copy_service_data
from unshackle.core.utilities import close_debug_logger, init_debug_logger
from unshackle.core.vault import brief
from unshackle.core.vaults import Vaults
from unshackle.vaults.API import API
from unshackle.vaults.HTTP import HTTP
from unshackle.vaults.SQLite import SQLite

KID = UUID("11111111-2222-3333-4444-555555555555")
SIBLING = UUID("99999999-2222-3333-4444-555555555555")
URL = "http://u:SECRETPW@vault.example:8080/vault.php?username=u&password=SECRETPW&kid=00"
REFUSED = (
    "HTTPConnectionPool(host='vault.example', port=8080): Max retries exceeded with url: "
    '/vault.php?username=u&password=SECRETPW (Caused by NewConnectionError("<urllib3.connection.HTTPConnection '
    'object at 0x7f>: Failed to establish a new connection: [Errno 111] Connection refused"))'
)


def request() -> requests.PreparedRequest:
    return requests.Request("GET", URL).prepare()


def html(status: int = 200) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.headers["Content-Type"] = "text/html"
    r._content = b"<html><body>parked</body></html>"
    r.request = request()
    return r


def test_brief_keeps_the_host_and_reason_and_drops_the_url() -> None:
    text = brief(requests.ConnectionError(REFUSED, request=request()))
    assert "vault.example:8080" in text
    assert "Connection refused" in text
    for leak in ("SECRETPW", "?", "/vault.php", "\n"):
        assert leak not in text


def test_brief_names_a_timeout() -> None:
    error = requests.ReadTimeout(
        "HTTPConnectionPool(host='vault.example', port=8080): Read timed out. (read timeout=10)", request=request()
    )
    text = brief(error)
    assert "timed out" in text
    assert "SECRETPW" not in text


def test_brief_gives_the_status_of_an_http_error() -> None:
    response = html(503)
    error = requests.HTTPError(f"503 Server Error: Service Unavailable for url: {URL}", response=response)
    assert brief(error) == "HTTP 503 from vault.example:8080"


def test_brief_keeps_one_short_line_of_any_other_error() -> None:
    assert brief(ValueError("line1\nline2")) == "line1"
    assert len(brief(ValueError("x" * 5000))) <= 200
    assert brief(ValueError()) == "ValueError"


def test_brief_keeps_the_reason_of_a_tls_failure() -> None:
    error = requests.exceptions.SSLError(
        "HTTPSConnectionPool(host='vault.example', port=8080): Max retries exceeded with url: /vault.php?password=SECRETPW "
        "(Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "self-signed certificate (_ssl.c:1010)')))",
        request=request(),
    )
    assert brief(error) == "SSLError to vault.example:8080 (certificate verify failed: self-signed certificate)"


def test_brief_keeps_the_text_of_a_requests_error_with_no_request() -> None:
    assert brief(requests.JSONDecodeError("Expecting value", "<html>", 0)).startswith("Expecting value")
    text = brief(requests.exceptions.InvalidSchema(f"No connection adapters were found for 'htp{URL[4:]}'"))
    assert text.startswith("No connection adapters were found")
    assert "SECRETPW" not in text


def test_brief_masks_a_password_in_the_text_of_any_other_error() -> None:
    assert "SECRETPW" not in brief(ValueError(f"bad answer from {URL}"))


def records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def test_a_query_mode_vault_that_is_down_never_logs_its_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def down(*args: object, **kw: object) -> None:
        raise requests.ConnectionError(REFUSED, request=request())

    vault = HTTP("http", "http://vault.example:8080/vault.php", password="SECRETPW", username="u")
    monkeypatch.setattr(vault.session, "get", down)
    vaults = Vaults("SVC")
    vaults.vaults = [vault]

    debug_log = tmp_path / "debug.jsonl"
    init_debug_logger(log_path=debug_log, enabled=True)
    try:
        with caplog.at_level(logging.DEBUG):
            assert vaults.get_key(KID) == (None, None)
            assert vaults.get_key(SIBLING) == (None, None)
    finally:
        close_debug_logger()

    assert "vault_get_key" in debug_log.read_text()
    assert "SECRETPW" not in debug_log.read_text()

    assert not [m for m in (r.getMessage() for r in caplog.records) if "SECRETPW" in m]
    assert len([m for m in records(caplog, logging.WARNING) if m.startswith("Failed to get key")]) == 1
    assert vault in vaults.unreachable


def test_an_api_vault_that_answers_html_is_skipped_after_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []

    def parked(url: str, **kw: object) -> requests.Response:
        calls.append(url)
        return html()

    vault = API("api", "https://api.example/", "tok")
    monkeypatch.setattr(vault.session, "get", parked)
    monkeypatch.setattr(vault.session, "post", parked)
    vaults = Vaults("SVC")
    vaults.vaults = [vault]

    with caplog.at_level(logging.DEBUG):
        assert vaults.get_key(KID) == (None, None)
        assert vaults.get_key(SIBLING) == (None, None)

    assert len(calls) == 1
    assert vault in vaults.unreachable
    warnings = [m for m in records(caplog, logging.WARNING) if m.startswith("Failed to get key")]
    assert warnings == ["Failed to get key from Vault 'api': HTTP 200 text/html response is not a JSON object"]
    assert not [r for r in caplog.records if "<html" in r.getMessage()]
    with pytest.raises(ConnectionError):
        vault.add_keys("EXAMPLE", {KID: "bb" * 16})
    assert len(calls) == 2


def test_a_broken_sqlite_file_warns_once_and_is_still_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "local.db"
    path.write_bytes(b"<html><body>not a database</body></html>" * 100)
    vault = SQLite("local", path)
    queried: list[UUID | str] = []
    get_key = vault.get_key

    def counted(kid: UUID | str, service: str) -> str | None:
        queried.append(kid)
        return get_key(kid, service)

    monkeypatch.setattr(vault, "get_key", counted)
    vaults = Vaults("SVC")
    vaults.vaults = [vault]

    with caplog.at_level(logging.DEBUG):
        assert vaults.get_key(KID) == (None, None)
        assert vaults.get_key(SIBLING) == (None, None)
        assert vaults.add_keys({KID: "bb" * 16}) == 0

    warnings = records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "file is not a database" in warnings[0]
    assert len([m for m in records(caplog, logging.DEBUG) if "file is not a database" in m]) == 2
    assert vault not in vaults.unreachable
    assert queried == [KID, SIBLING]


def test_kv_copy_names_the_vault_it_cannot_write_to_without_its_password(tmp_path: Path) -> None:
    class Down(SQLite):
        def add_keys(self, service: str, kid_keys: dict[UUID | str, str]) -> int:
            raise requests.ConnectionError(REFUSED, request=request())

    source = SQLite("source", tmp_path / "source.db")
    source.add_key("EXAMPLE", KID, "bb" * 16)
    with pytest.raises(click.ClickException) as raised:
        copy_service_data(Down("down", tmp_path / "down.db"), source, "EXAMPLE", logging.getLogger("kv"))
    assert (
        raised.value.message
        == "Could not write to down Down (ConnectionError to vault.example:8080 (Connection refused))"
    )
