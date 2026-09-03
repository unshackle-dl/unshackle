"""Server-wide event broadcaster feeding the dashboard SSE event stream."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from contextlib import suppress
from typing import Any, Deque, Dict, List, Optional


class EventBus:
    """Fan events out to every subscriber queue; drop when a subscriber falls behind."""

    def __init__(self, history: int = 20000) -> None:
        self._subs: List[asyncio.Queue] = []
        self.seq = 0
        self.history: Deque[Dict[str, Any]] = deque(maxlen=history)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None

    def subscribe(self) -> asyncio.Queue:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.current_thread()
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with suppress(ValueError):
            self._subs.remove(queue)

    def since(self, seq: int) -> List[Dict[str, Any]]:
        return [item for item in self.history if item["seq"] > seq]

    def publish(self, event: str, data: Dict[str, Any]) -> None:
        self.seq += 1
        item = {"seq": self.seq, "event": event, "data": data}
        self.history.append(item)
        if not self._subs:
            return
        on_loop = self._loop is None or threading.current_thread() is self._loop_thread
        for queue in list(self._subs):
            if on_loop:
                _put_drop(queue, item)
            else:
                assert self._loop is not None
                self._loop.call_soon_threadsafe(_put_drop, queue, item)


def _put_drop(queue: asyncio.Queue, item: Dict[str, Any]) -> None:
    with suppress(asyncio.QueueFull):
        queue.put_nowait(item)


bus = EventBus()


def publish_service_event(action: str, tags: List[str], errors: Optional[List[str]] = None) -> None:
    """Tell dashboard listeners that services were staged, applied, or failed to reload.

    Lives here rather than in ``core.services`` so the service loader never imports the API layer.
    """
    data: Dict[str, Any] = {"action": action, "tags": list(tags)}
    if errors:
        data["errors"] = list(errors)
    bus.publish("service", data)


def publish_refresh_events(repos: List[Dict[str, Any]]) -> None:
    """Publish the ``service`` events one ``refresh_and_reload`` result implies.

    A tag counts as applied only when it changed, was not deferred, and did not fail to
    import - a failed re-import keeps the old module, so calling it applied would be a lie.
    """
    from unshackle.core.services import failed_tags

    for repo in repos:
        failed = failed_tags(repo["load_errors"])
        changed = {line[1:] for line in repo["changes"] if line and line[0] in "+~-!"}
        applied = sorted(changed - set(repo["deferred"]) - failed)
        if applied:
            publish_service_event("applied", applied)
        if repo["deferred"]:
            publish_service_event("staged", repo["deferred"])
        if repo["load_errors"]:
            publish_service_event("failed", [], errors=repo["load_errors"])
