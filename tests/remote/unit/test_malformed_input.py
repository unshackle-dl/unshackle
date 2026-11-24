"""Malformed input from a client or a server gets a one-line refusal, never an internal error or a traceback."""

from __future__ import annotations

import base64
import logging
from collections import deque
from threading import Lock
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from aiohttp import web
from click.testing import CliRunner

from unshackle.core import remote_service
from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError, APIErrorCode
from unshackle.core.api.routes import setup_routes
from unshackle.core.remote_service import RemoteClient, RemoteError, RemoteService

pytestmark = pytest.mark.unit


async def test_a_body_that_is_not_an_object_is_invalid_input(aiohttp_client: Any) -> None:
    app = web.Application()
    app["config"] = {"users": {}}
    app["debug_api"] = False
    setup_routes(app)
    client = await aiohttp_client(app)
    for body in (b"[1, 2]", b"null", b'"x"', b"5"):
        resp = await client.post("/api/session/create", data=body, headers={"Content-Type": "application/json"})
        assert resp.status == 400
        assert (await resp.json())["error_code"] == "INVALID_INPUT"


def test_a_cache_value_that_is_not_compressed_is_invalid_input(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers.config.directories, "cache", tmp_path)
    with pytest.raises(APIError) as e:
        handlers.write_client_cache({"token": base64.b64encode(b"plain").decode()}, "tag")
    assert e.value.error_code == APIErrorCode.INVALID_INPUT


def test_a_challenge_that_is_not_base64_is_invalid_input() -> None:
    with pytest.raises(APIError) as e:
        handlers.handle_proxy_license(None, None, None, "!!notb64", "widevine")
    assert e.value.error_code == APIErrorCode.INVALID_INPUT


async def test_segments_refuse_track_ids_that_are_not_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def session(*_: Any) -> Any:
        return SimpleNamespace(tracks={})

    monkeypatch.setattr(handlers, "get_validated_session", session)
    monkeypatch.setattr(handlers, "require_authenticated", lambda _: None)
    with pytest.raises(APIError) as e:
        await handlers.session_segments_handler({"track_ids": [{"a": 1}]}, "sid")
    assert e.value.error_code == APIErrorCode.INVALID_INPUT


async def test_a_client_owner_key_does_not_reach_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.api import download_manager

    made: dict[str, Any] = {}

    class Manager:
        async def start_workers(self) -> None:
            return None

        def create_job(self, service: str, title_id: str, owner_key: Any = None, **params: Any) -> Any:
            made.update(params, owner=owner_key)
            raise RuntimeError("stop")

    monkeypatch.setattr(handlers, "validate_service", lambda tag, request=None: tag)
    monkeypatch.setattr(handlers.Services, "load", lambda tag: object())
    monkeypatch.setattr(download_manager, "get_download_manager", lambda: Manager())
    await handlers.download_handler({"service": "EXAMPLE", "title_id": "t", "owner_key": "OTHER"})
    assert made["owner"] == "anonymous"
    assert "owner_key" not in made


def remote_answer(monkeypatch: pytest.MonkeyPatch, text: str) -> RemoteClient:
    client = RemoteClient("http://server.example", "key")
    resp = requests.Response()
    resp.status_code = 200
    resp._content = text.encode()
    resp.headers["Content-Type"] = "application/json"
    monkeypatch.setattr(client.session, "get", lambda *a, **k: resp)
    return client


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"video": ['])
def test_a_server_answer_that_is_not_a_json_object_is_one_remote_error(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    with pytest.raises(RemoteError):
        remote_answer(monkeypatch, text).get("/api/session/s/titles")


@pytest.mark.parametrize("answer", [{"titles": ["x"]}, {"titles": [{"type": "episode", "season": "x"}]}])
def test_malformed_titles_from_the_server_are_one_remote_error(answer: dict[str, Any]) -> None:
    service = RemoteService.__new__(RemoteService)
    service._titles = None
    service.service_tag = "EXAMPLE"
    service.title_id = "t"
    service.session_call = lambda call: answer  # type: ignore[method-assign]
    service.drain_server_logs = lambda: None  # type: ignore[method-assign]
    with pytest.raises(RemoteError):
        service.get_titles()


@pytest.mark.parametrize("answer", [{"video": None}, {"video": [{"bitrate": "x1"}]}, {"attachments": [5]}])
def test_malformed_tracks_from_the_server_are_one_remote_error(answer: dict[str, Any]) -> None:
    from unshackle.core.titles import Movie

    service = RemoteService.__new__(RemoteService)
    service._tracks_by_title = {}
    service._title_alias = {}
    service.server_draining = lambda: False  # type: ignore[method-assign]
    service.session_call = lambda call: answer  # type: ignore[method-assign]
    service.drain_server_logs = lambda: None  # type: ignore[method-assign]
    with pytest.raises(RemoteError):
        service.get_tracks(Movie(id_="title-1", service=RemoteService, name="Name"))


async def test_full_mode_logs_no_rejected_key_and_answers_405(
    monkeypatch: pytest.MonkeyPatch, aiohttp_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from unshackle.commands.serve import serve
    from unshackle.core.config import config as cfg

    captured: dict[str, Any] = {}
    monkeypatch.setattr(web, "run_app", lambda app, **kwargs: captured.update(app=app))
    monkeypatch.setattr(cfg, "serve", {"api_secret": "admin-key", "users": {"user-key": {"username": "a"}}})
    result = CliRunner().invoke(serve, ["--no-playready"])
    assert result.exit_code == 0, result.output
    client = await aiohttp_client(captured["app"])
    with caplog.at_level(logging.DEBUG):
        resp = await client.get("/api/services", headers={"X-Secret-Key": "WRONG-key-example"})
    assert resp.status == 401
    assert "WRONG-key-example" not in caplog.text
    assert (await client.put("/api/download", headers={"X-Secret-Key": "user-key"})).status == 405


@pytest.mark.parametrize("title_id", [5, ["a"]])
async def test_a_title_id_that_is_not_a_string_is_invalid_input(title_id: Any) -> None:
    with pytest.raises(APIError) as e:
        await handlers.download_handler({"service": "EXAMPLE", "title_id": title_id})
    assert e.value.error_code == APIErrorCode.INVALID_INPUT


@pytest.mark.parametrize("wanted", ["zzz", "S01E05-S01E02", [5], {"a": 1}])
async def test_garbage_wanted_is_invalid_input_at_submit(wanted: Any) -> None:
    with pytest.raises(APIError) as e:
        await handlers.download_handler({"service": "EXAMPLE", "title_id": "t", "wanted": wanted})
    assert e.value.error_code == APIErrorCode.INVALID_INPUT


@pytest.mark.parametrize("wanted", ["S01E01", "s1e1-s1e3, 2x4", ["1x1", "!1x1.2"]])
def test_good_wanted_passes_at_submit(wanted: Any) -> None:
    assert handlers.wanted_refusal(wanted) is None


def test_a_job_error_hides_the_configured_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    from unshackle.core.api.download_manager import DownloadJob, JobStatus

    monkeypatch.setattr(handlers.config, "credentials", {"EXAMPLE": {"default": "user:EXAMPLE-pass-123"}})
    job = DownloadJob("j1", JobStatus.FAILED, datetime(2026, 1, 1), "EXAMPLE", "t", {})
    job.error_message = "login failed with EXAMPLE-pass-123"
    assert "EXAMPLE-pass-123" not in job.to_dict(include_full_details=True)["error_message"]


def test_decrypting_without_content_keys_is_one_line(tmp_path: Any) -> None:
    import click

    from unshackle.core.drm.decrypters import decrypt_cenc

    with pytest.raises(click.ClickException):
        decrypt_cenc(SimpleNamespace(content_keys={}), tmp_path / "x.mp4", "Widevine")


def test_a_session_create_answer_without_a_session_id_is_one_remote_error() -> None:
    with pytest.raises(RemoteError, match="malformed session"):
        with remote_service.malformed_answer("session"):
            raise TypeError("session_id is not a string")


def test_server_text_loses_its_control_characters() -> None:
    error = RemoteError("Server error [X]: \x1b[31mred\x1b[0m\r\n[forged] INFO ok", "UNKNOWN")
    assert "\x1b" not in error.message and "\n" not in error.message and "\r" not in error.message


def test_relayed_server_logs_lose_their_control_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    service = RemoteService.__new__(RemoteService)
    service._session_id = "sid"
    service._log_seq = 0
    service._recent_logs = deque(maxlen=5)
    service._log_drain_lock = Lock()
    seen: list[str] = []
    service.log = SimpleNamespace(log=lambda level, message: seen.append(message))  # type: ignore[assignment]
    service.client = SimpleNamespace(
        get_optional=lambda _: {"logs": [{"level": "INFO", "message": "a\x1b[2Jb\nc"}], "last_seq": 1}
    )
    service.drain_server_logs()
    assert seen == ["a[2Jb\nc"]
