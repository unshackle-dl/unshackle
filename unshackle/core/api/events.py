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
