"""One remote session owns one service instance, so its handlers must take turns.

``session_titles_handler`` and ``session_tracks_handler`` call the service from a worker
thread. Both calls reach the same long-lived ``SessionEntry.service_instance``, and services
keep per-title state on it: HTTP session headers, per-title tokens, cached manifest
attributes. Two requests on one remote session that run in that instance at the same time
corrupt each other's state, and the track response then hands the client the wrong headers.

The probe here reports how many service calls are inside the instance at once. Its first
entrant waits a short window for company, so the test sees a genuinely concurrent run instead
of racing past it. A slow machine can only make a concurrent run look serialised, so these
tests cannot flake red.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from types import SimpleNamespace
from typing import Any, Iterator, List

import pytest

from unshackle.core.api import handlers
from unshackle.core.api.session_store import SessionEntry, get_session_store
from unshackle.core.titles.movie import Movie

pytestmark = pytest.mark.unit

# How long the first service call waits for a second one to join it.
OVERLAP_WINDOW = 0.5

# Bounds the wait when the loop is blocked, so a regression fails the test instead of hanging.
HANDSHAKE_TIMEOUT = 5.0


class Overlap:
    """Records how many service calls are inside the service instance at once."""

    def __init__(self, window: float = OVERLAP_WINDOW) -> None:
        self.window = window
        self.max_active = 0
        self.trace: List[str] = []
        self._lock = threading.Lock()
        self._active = 0
        self._joined = threading.Event()
        self._waited = False

    @contextlib.contextmanager
    def call(self, label: str) -> Iterator[None]:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.trace.append(f"+{label}")
            if self._active > 1:
                self._joined.set()
            first = not self._waited
            self._waited = True
        # Only the first call pays the window. Serialised handlers then cost one wait, not one
        # per call, and a concurrent second call releases it as soon as it arrives.
        if first:
            self._joined.wait(timeout=self.window)
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
                self.trace.append(f"-{label}")


def a_movie(title_id: str) -> Movie:
    return Movie(id_=title_id, service=object, name=f"Film {title_id}", year=2024, language=None)


def empty_tracks() -> Any:
    return SimpleNamespace(videos=[], audio=[], subtitles=[], attachments=[])


class ServiceProbe:
    """A service whose calls report their overlap into a shared recorder."""

    def __init__(self, overlap: Overlap, name: str = "probe") -> None:
        self.overlap = overlap
        self.name = name
        self.session = SimpleNamespace(headers={}, cookies=[])

    def get_titles(self) -> List[Movie]:
        with self.overlap.call(f"{self.name}:titles"):
            return [a_movie("movie-0001"), a_movie("movie-0002")]

    def get_tracks(self, title: Any) -> Any:
        with self.overlap.call(f"{self.name}:tracks:{title.id}"):
            return empty_tracks()

    def get_chapters(self, title: Any) -> List[Any]:
        with self.overlap.call(f"{self.name}:chapters:{title.id}"):
            return []


def seed_titles(session: SessionEntry) -> None:
    """Put both titles in the remote session, as a prior titles call would."""
    for title_id in ("movie-0001", "movie-0002"):
        session.title_map[title_id] = a_movie(title_id)


def bodies(responses: List[Any]) -> List[dict]:
    parsed = [json.loads(response.body) for response in responses]
    for body in parsed:
        assert "error" not in body, body
    return parsed


async def two_track_calls(session_id: str) -> List[Any]:
    return list(
        await asyncio.gather(
            handlers.session_tracks_handler({"title_id": "movie-0001"}, session_id),
            handlers.session_tracks_handler({"title_id": "movie-0002"}, session_id),
        )
    )


@pytest.mark.asyncio
async def test_two_track_requests_never_share_the_service() -> None:
    overlap = Overlap()
    store = get_session_store()
    session = await store.create("EXAMPLE", ServiceProbe(overlap), session_id="lock-test-tracks")
    seed_titles(session)
    try:
        responses = await two_track_calls(session.session_id)
    finally:
        await store.delete(session.session_id)

    bodies(responses)
    assert overlap.max_active == 1, overlap.trace


@pytest.mark.asyncio
async def test_the_track_lock_covers_the_chapters_call() -> None:
    overlap = Overlap()
    store = get_session_store()
    session = await store.create("EXAMPLE", ServiceProbe(overlap), session_id="lock-test-chapters")
    seed_titles(session)
    try:
        responses = await two_track_calls(session.session_id)
    finally:
        await store.delete(session.session_id)

    bodies(responses)
    # Every call closes before the next one opens, so one title's chapters are read before the
    # other title's tracks are fetched.
    assert len(overlap.trace) == 8, overlap.trace
    for opened, closed in zip(overlap.trace[0::2], overlap.trace[1::2]):
        assert closed == f"-{opened[1:]}", overlap.trace


@pytest.mark.asyncio
async def test_titles_and_tracks_never_overlap() -> None:
    overlap = Overlap()
    store = get_session_store()
    session = await store.create("EXAMPLE", ServiceProbe(overlap), session_id="lock-test-mixed")
    seed_titles(session)
    try:
        responses = list(
            await asyncio.gather(
                handlers.session_titles_handler(session.session_id),
                handlers.session_tracks_handler({"title_id": "movie-0001"}, session.session_id),
            )
        )
    finally:
        await store.delete(session.session_id)

    bodies(responses)
    assert overlap.max_active == 1, overlap.trace


@pytest.mark.asyncio
async def test_a_second_session_still_runs_in_parallel() -> None:
    overlap = Overlap()
    store = get_session_store()
    first = await store.create("EXAMPLE", ServiceProbe(overlap, "a"), session_id="lock-test-parallel-a")
    second = await store.create("EXAMPLE", ServiceProbe(overlap, "b"), session_id="lock-test-parallel-b")
    try:
        responses = list(
            await asyncio.gather(
                handlers.session_titles_handler(first.session_id),
                handlers.session_titles_handler(second.session_id),
            )
        )
    finally:
        await store.delete(first.session_id)
        await store.delete(second.session_id)

    bodies(responses)
    assert first.lock is not second.lock
    assert overlap.max_active == 2, overlap.trace


class BlockingTracks:
    """Holds ``get_tracks`` open until the event loop releases it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.loop_was_blocked = False
        self.session = SimpleNamespace(headers={}, cookies=[])

    def get_tracks(self, title: Any) -> Any:
        self.entered.set()
        self.loop_was_blocked = not self.resume.wait(timeout=HANDSHAKE_TIMEOUT)
        return empty_tracks()

    def get_chapters(self, title: Any) -> List[Any]:
        return []


@pytest.mark.asyncio
async def test_the_loop_stays_free_while_the_lock_is_held() -> None:
    service = BlockingTracks()
    store = get_session_store()
    session = await store.create("EXAMPLE", service, session_id="lock-test-loop")
    seed_titles(session)
    try:
        task = asyncio.create_task(handlers.session_tracks_handler({"title_id": "movie-0001"}, session.session_id))
        try:
            # Reached only while the loop is free, which is what the lock placement buys.
            await asyncio.to_thread(service.entered.wait, HANDSHAKE_TIMEOUT)
            held = session.lock.locked()
        finally:
            service.resume.set()
            response = await asyncio.wait_for(task, HANDSHAKE_TIMEOUT)
    finally:
        await store.delete(session.session_id)

    bodies([response])
    assert not service.loop_was_blocked, "get_tracks blocked the event loop"
    assert held, "the session lock was not held around the threaded service call"


@pytest.mark.asyncio
async def test_each_session_entry_gets_its_own_lock() -> None:
    store = get_session_store()
    first = await store.create("EXAMPLE", object(), session_id="lock-test-field-a")
    second = await store.create("EXAMPLE", object(), session_id="lock-test-field-b")
    try:
        assert isinstance(first.lock, asyncio.Lock)
        assert isinstance(second.lock, asyncio.Lock)
        assert first.lock is not second.lock
    finally:
        await store.delete(first.session_id)
        await store.delete(second.session_id)
