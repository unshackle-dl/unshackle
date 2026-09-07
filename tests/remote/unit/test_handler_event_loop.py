"""Blocking service calls must not run on the aiohttp event loop.

get_titles and get_tracks do network work that can take a whole manifest round trip. Run on
the loop, they stop the server: the progress pump and the SSE keep-alive stall with them. An
interactive prompt from the service can never be answered either, because the endpoint that
answers it needs the same loop. Each test here holds the service call in a two-event
handshake that only completes while the loop still runs another coroutine.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, List

import pytest
from aiohttp import web

from unshackle.core.api import handlers
from unshackle.core.api.session_store import get_session_store
from unshackle.core.titles.movie import Movie

pytestmark = pytest.mark.unit

# Bounds the wait when the loop is blocked, so a regression fails the test instead of hanging.
HANDSHAKE_TIMEOUT = 5.0


class Handshake:
    """A blocking call that reports whether the event loop kept running during it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.loop_was_blocked = False

    def block(self) -> None:
        self.entered.set()
        self.loop_was_blocked = not self.resume.wait(timeout=HANDSHAKE_TIMEOUT)

    async def drive(self, call: Callable[[], Awaitable[web.Response]]) -> web.Response:
        task = asyncio.create_task(call())
        # Reached only while the loop is free.
        await asyncio.to_thread(self.entered.wait, HANDSHAKE_TIMEOUT)
        self.resume.set()
        return await asyncio.wait_for(task, HANDSHAKE_TIMEOUT)


def a_movie() -> Movie:
    return Movie(id_="movie-0001", service=object, name="Film", year=2024, language=None)


class BlockingTitles:
    def __init__(self, handshake: Handshake) -> None:
        self.handshake = handshake

    def get_titles(self) -> List[Movie]:
        self.handshake.block()
        return [a_movie()]


class BlockingTracks:
    def __init__(self, handshake: Handshake) -> None:
        self.handshake = handshake

    def get_titles(self) -> List[Movie]:
        return [a_movie()]

    def get_tracks(self, title: Any) -> Any:
        self.handshake.block()
        return SimpleNamespace(videos=[], audio=[], subtitles=[])


@pytest.mark.asyncio
async def test_session_titles_leaves_the_loop_free() -> None:
    handshake = Handshake()
    store = get_session_store()
    await store.create("EXAMPLE", BlockingTitles(handshake), session_id="loop-test-titles")
    try:
        response = await handshake.drive(lambda: handlers.session_titles_handler("loop-test-titles"))
    finally:
        await store.delete("loop-test-titles")

    assert not handshake.loop_was_blocked, "get_titles blocked the event loop"
    assert [t["name"] for t in json.loads(response.body)["titles"]] == ["Film"]


@pytest.mark.asyncio
async def test_list_tracks_leaves_the_loop_free(monkeypatch: pytest.MonkeyPatch) -> None:
    handshake = Handshake()
    service = BlockingTracks(handshake)
    monkeypatch.setattr(handlers, "validate_service", lambda tag, request=None: "EXAMPLE")
    monkeypatch.setattr(handlers, "setup_list_service", lambda *args, **kwargs: service)

    data = {"service": "EXAMPLE", "title_id": "movie-0001"}
    response = await handshake.drive(lambda: handlers.list_tracks_handler(data))

    assert not handshake.loop_was_blocked, "get_tracks blocked the event loop"
    assert json.loads(response.body)["title"]["name"] == "Film"
