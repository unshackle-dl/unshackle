"""The messages a remote session's server starts, and the session channel that carries them to the client.

Service threads (prompts and CDM calls in the input bridge, the session log mirror), the drain and the session
store hand each message to the remote session's :class:`SessionChannel`. While the client holds the channel's
WebSocket open, the channel sends the message at once. Otherwise the message waits where the polling routes read
it (the input bridge, the log buffer of the remote session, the drain flag), as for a client without the channel.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

from aiohttp import WSMsgType, web

from unshackle.core.api.sanitize import sanitize_log
from unshackle.core.remote_protocol import (
    CHANNEL_SESSION_GONE,
    ChannelAnswer,
    ChannelCdmCall,
    ChannelDrain,
    ChannelLog,
    ChannelMessage,
    ChannelPrompt,
    ChannelStatus,
    LogRecord,
)
from unshackle.core.tunnel import HEARTBEAT

if TYPE_CHECKING:
    from unshackle.core.api.input_bridge import Ask
    from unshackle.core.api.session_store import SessionEntry

Message = ChannelPrompt | ChannelCdmCall | ChannelLog | ChannelStatus | ChannelDrain
GONE = object()


def drain_message(draining: bool) -> ChannelDrain:
    return ChannelDrain(type=ChannelMessage.DRAIN, draining=draining)


def status_message(session: SessionEntry) -> ChannelStatus:
    """The login state of the remote session, with the same redacted error that the prompt route gives."""
    message = ChannelStatus(type=ChannelMessage.STATUS, status=session.auth_status.value)
    if session.auth_error:
        message["error"] = session.auth_error
    return message


class SessionChannel:
    """The WebSocket of one remote session, while the client holds it open.

    Any thread can hand a message to :meth:`send`: it goes to the event loop through ``call_soon_threadsafe``,
    and one writer task sends the messages in order. Nothing here takes a lock or waits on a worker.
    """

    def __init__(self) -> None:
        self.ws: web.WebSocketResponse | None = None
        self.queue: asyncio.Queue[Any] | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ended = False

    @property
    def attached(self) -> bool:
        return self.ws is not None

    def send(self, message: Any) -> bool:
        """Queue *message* for the attached WebSocket, from any thread; False when no WebSocket is attached."""
        loop, queue = self.loop, self.queue
        if loop is None or queue is None:
            return False
        try:
            loop.call_soon_threadsafe(queue.put_nowait, message)
        except RuntimeError:  # the loop closed at server shutdown
            return False
        return True

    def offer(self, ask: Ask) -> bool:
        """Send a prompt or CDM call that service code waits on; False when the polling routes must serve it."""
        return self.send(ask_message(ask))

    def log(self, record: LogRecord) -> None:
        """Send one record of the service log mirror; the buffer keeps it for the logs route too."""
        if self.queue is not None:
            self.send(log_message(record))

    def end(self) -> None:
        """Close the channel for good, with the close code that tells the client the remote session is gone."""
        self.ended = True
        self.send(GONE)

    async def serve(self, ws: web.WebSocketResponse, session: SessionEntry, since: int, draining: bool) -> None:
        """Attach *ws* until the client or the server closes it.

        The client first gets what it missed: the log records after *since*, a drain notice, the prompts and CDM
        calls the polling routes did not serve, then the login state. A log record can come twice; its ``seq``
        tells the client.
        """
        if self.ended:
            await ws.close(code=CHANNEL_SESSION_GONE, message=b"session closed")
            return
        previous = self.ws
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self.loop, self.ws, self.queue = asyncio.get_running_loop(), ws, queue
        backlog: list[Any] = []
        if session.log_buffer is not None:
            backlog += [log_message(r) for r in session.log_buffer.since(since)]
        backlog.append(drain_message(draining))
        bridge = session.input_bridge
        backlog += [ask_message(ask) for ask in (bridge.claim_for_channel() if bridge else [])]
        backlog.append(status_message(session))
        for message in backlog:
            queue.put_nowait(message)
        writer = asyncio.create_task(self.write(ws, queue, session))
        try:
            if previous is not None:
                await previous.close()
            async for msg in ws:
                if msg.type == WSMsgType.TEXT and bridge is not None:
                    receive(msg.data, bridge)
        finally:
            writer.cancel()
            if self.ws is ws:
                self.ws = self.queue = None
                if bridge is not None:
                    bridge.release_channel_asks()
            session.touch()

    @staticmethod
    async def write(ws: web.WebSocketResponse, queue: asyncio.Queue[Any], session: SessionEntry) -> None:
        """Send the queued messages in order; the idle timer moves while the channel is open, for the dashboard."""
        try:
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), HEARTBEAT)
                except TimeoutError:
                    session.touch()
                    continue
                if message is GONE:
                    await ws.close(code=CHANNEL_SESSION_GONE, message=b"session closed")
                    return
                await ws.send_json(message)
                session.touch()
        except Exception:  # a writer that cannot send closes the socket, so the session does not stay attached
            await ws.close()


def log_message(record: LogRecord) -> ChannelLog:
    return cast(ChannelLog, {"type": ChannelMessage.LOG, **record})


def ask_message(ask: Ask) -> ChannelPrompt | ChannelCdmCall:
    if ask.cdm_call is not None:
        return ChannelCdmCall(type=ChannelMessage.CDM_CALL, id=ask.id, call=ask.cdm_call)
    return ChannelPrompt(type=ChannelMessage.PROMPT, id=ask.id, prompt=ask.prompt)


def receive(text: str, bridge: Any) -> None:
    """Hand the client's answer to the ask it names; any other message is ignored."""
    try:
        data: ChannelAnswer = json.loads(text)
    except ValueError:
        return
    if (
        isinstance(data, dict)
        and data.get("type") == ChannelMessage.ANSWER
        and isinstance(data.get("id"), str)
        and isinstance(data.get("response"), str)
    ):
        error = data.get("error")
        bridge.answer(data["id"], data["response"], sanitize_log(error)[:500] if isinstance(error, str) else None)


async def close_channels(_: web.Application) -> None:
    """End every attached channel at server shutdown, so no WebSocket holds the shutdown open."""
    from unshackle.core.api.session_store import get_session_store

    for entry in get_session_store().list():
        entry.channel.end()
