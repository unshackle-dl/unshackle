"""The dashboard's view of a remote session: the live title, and the client's redacted command line."""

import asyncio
import shlex
from types import SimpleNamespace
from typing import Any

import pytest

from unshackle.core.api import session_store as store_mod
from unshackle.core.api.handlers import find_title_for_track, session_tracks_handler
from unshackle.core.api.session_store import SessionEntry
from unshackle.core.tracks import Tracks
from unshackle.core.utils.redact import redact_text


class FakeTitle:
    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name


def make_entry() -> SessionEntry:
    entry = SessionEntry(session_id="s1", service_tag="EXAMPLE", service_instance=None)
    entry.title_map = {"t1": FakeTitle("Example Show 2024 S01E01"), "t3": FakeTitle("Example Show 2024 S01E03")}
    return entry


def test_summary_falls_back_to_the_first_title() -> None:
    summary = make_entry().summary()
    assert summary["title_id"] == "t1"
    assert summary["title"] == "Example Show 2024 S01E01"


def test_summary_reports_the_title_the_client_asked_for() -> None:
    entry = make_entry()
    entry.current_title_id = "t3"
    summary = entry.summary()
    assert summary["title_id"] == "t3"
    assert summary["title"] == "Example Show 2024 S01E03"


def test_summary_ignores_a_title_id_that_left_the_map() -> None:
    entry = make_entry()
    entry.current_title_id = "gone"
    assert entry.summary()["title_id"] == "t1"


def test_unknown_track_resolves_to_the_live_title() -> None:
    entry = make_entry()
    entry.current_title_id = "t3"
    assert find_title_for_track("nope", entry) is entry.title_map["t3"]


def argv_line(argv: list[str], credentials: dict[str, str] | None = None) -> str:
    """Mirror the argv capture in RemoteService.authenticate."""
    secrets = [v for v in (credentials or {}).values() if isinstance(v, str) and len(v) >= 4]
    return redact_text(shlex.join(argv), secrets) or ""


def test_argv_keeps_the_flags_and_drops_the_proxy_password() -> None:
    line = argv_line(
        [
            "dl",
            "-w",
            "s1e1-s1e4",
            "-r",
            "HDR10",
            "-v",
            "H.264,H.265",
            "--list",
            "--proxy",
            "http://bob:hunter2@proxy.example.com:8080",
            "--remote",
            "EXAMPLE",
            "https://example.com/title/1",
        ]
    )
    assert "hunter2" not in line
    assert "bob" not in line
    assert "***@proxy.example.com:8080" in line
    assert "-w s1e1-s1e4" in line
    assert "-r HDR10" in line
    assert "--list" in line


def test_argv_drops_credential_values_and_url_tokens() -> None:
    line = argv_line(
        ["dl", "--remote", "EXAMPLE", "https://example.com/t?token=abc123"],
        credentials={"username": "bob", "password": "hunter2", "extra": "totp-seed"},
    )
    assert "hunter2" not in line
    assert "totp-seed" not in line
    assert "abc123" not in line
    assert "token=***" in line


def test_argv_does_not_mask_a_short_credential_value() -> None:
    line = argv_line(
        ["dl", "-w", "s1e1", "--remote", "EXAMPLE", "t1"], credentials={"username": "s1", "password": "e1"}
    )
    assert "-w s1e1" in line


@pytest.mark.parametrize("param", ["access_token", "auth_token", "client_secret", "api-key", "X-Api-Key"])
def test_argv_masks_affixed_secret_query_params(param: str) -> None:
    line = argv_line(["dl", "--remote", "EXAMPLE", f"https://example.com/t?{param}=abc123&kind=movie"])
    assert "abc123" not in line
    assert f"{param}=***" in line
    assert "kind=movie" in line


def test_tracks_handler_marks_the_title_live_before_it_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard should see the new title while get_tracks runs, not a title later."""
    published: list[tuple[str, dict[str, Any]]] = []
    seen_during_fetch: list[str | None] = []
    monkeypatch.setattr(store_mod.bus, "publish", lambda topic, data: published.append((topic, data)))

    class StubService:
        session = SimpleNamespace(headers={}, cookies=[])

        def get_tracks(self, title: Any) -> Tracks:
            seen_during_fetch.append(published[-1][1]["title"] if published else None)
            return Tracks()

        def get_chapters(self, title: Any) -> None:
            raise NotImplementedError

    async def run() -> None:
        store = store_mod.get_session_store()
        entry = await store.create(
            service_tag="EXAMPLE", service_instance=StubService(), session_id="live", owner_key=None
        )
        entry.title_map = {"t1": FakeTitle("Example Show 2024 S01E01"), "t3": FakeTitle("Example Show 2024 S01E03")}
        published.clear()
        resp = await session_tracks_handler({"title_id": "t3"}, "live")
        assert resp.status == 200, resp.text
        assert seen_during_fetch == ["Example Show 2024 S01E03"]
        assert entry.current_title_id == "t3"
        assert entry.summary()["title"] == "Example Show 2024 S01E03"
        await store.delete("live")

    asyncio.run(run())
