from __future__ import annotations

from copy import deepcopy
from enum import Enum
from typing import Any, Callable


class Events:
    class Types(Enum):
        _reserved = 0
        SEGMENT_DOWNLOADED = 1
        TRACK_DOWNLOADED = 2
        TRACK_DECRYPTED = 3
        TRACK_REPACKED = 4
        # Track is about to be Multiplexed into a Container
        TRACK_MULTIPLEX = 5

    def __init__(self):
        self.__subscriptions: dict[Events.Types, list[Callable]] = {}
        self.__ephemeral: dict[Events.Types, list[Callable]] = {}
        self.reset()

    def reset(self):
        """Reset Event Observer clearing all Subscriptions."""
        self.__subscriptions = {k: [] for k in Events.Types.__members__.values()}
        self.__ephemeral = deepcopy(self.__subscriptions)

    def subscribe(self, event_type: Events.Types, callback: Callable, ephemeral: bool = False) -> None:
        """
        Subscribe to an Event with a Callback.

        Parameters:
            event_type: The Events.Type to subscribe to.
            callback: The function or lambda to call when `emit()` sends the event.
            ephemeral: Unsubscribe the callback from the event at the first `emit()` call.
                Note that this is not thread-safe, so unshackle can call the
                callback several times at roughly the same time.
        """
        [self.__subscriptions, self.__ephemeral][ephemeral][event_type].append(callback)

    def unsubscribe(self, event_type: Events.Types, callback: Callable) -> None:
        """
        Unsubscribe a Callback from an Event.

        Parameters:
            event_type: The Events.Type to unsubscribe from.
            callback: The function or lambda to remove from the `emit()` callbacks.
        """
        if callback in self.__subscriptions[event_type]:
            self.__subscriptions[event_type].remove(callback)
        if callback in self.__ephemeral[event_type]:
            self.__ephemeral[event_type].remove(callback)

    def emit(self, event_type: Events.Types, *args: Any, **kwargs: Any) -> None:
        """
        Send an Event to all subscribed Callbacks.

        Parameters:
            event_type: The Events.Type to send.
            args: Positional arguments to pass to callbacks.
            kwargs: Keyword arguments to pass to callbacks.
        """
        if event_type not in self.__subscriptions:
            raise ValueError(f'Event type "{event_type}" is invalid')

        for callback in self.__subscriptions[event_type] + self.__ephemeral[event_type]:
            callback(*args, **kwargs)

        self.__ephemeral[event_type].clear()


events = Events()
