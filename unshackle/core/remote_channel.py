"""The messages a remote server starts for a remote session, on the client.

The server starts some messages itself: a login prompt, a call on this machine's CDM, a service log record, a
login state change and a drain notice. :func:`open_channel` gives :class:`RemoteService` one of two adapters with
the same methods. :class:`WebSocketChannel` gets the messages on the remote session channel. :class:`PollingChannel` asks
the server for them, when the channel upgrade is refused or fails, and while the channel is down.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import requests
from aiohttp import WSMsgType

from unshackle.core.console import prompt_user, show_notice, show_qr
from unshackle.core.remote_protocol import (
    CHANNEL_ROUTE,
    CHANNEL_SESSION_GONE,
    LOGS_ROUTE,
    PROMPT_ROUTE,
    SAMPLES_ROUTE,
    SESSION_ROUTE,
    AuthStatus,
    ChannelAnswer,
    ChannelMessage,
    ClientErrorKind,
    LogRecord,
    LogsReply,
    PromptAnswer,
    PromptReply,
    SessionInfo,
)
from unshackle.core.remote_service import RemoteError, quiet, ws_headers
from unshackle.core.tunnel import HEARTBEAT, retry_after

if TYPE_CHECKING:
    from unshackle.core.remote_service import RemoteService

log = logging.getLogger("remote_channel")

AUTH_TIMEOUT = 600.0
AUTH_POLL_INTERVAL = 2.0
CDM_POLL_INTERVAL = 0.5
KEEPALIVE_MIN_INTERVAL = 5.0
CONNECT_TIMEOUT = 10.0
RECONNECT_DELAY = 1.0
GONE_STATUSES = (401, 403, 404)  # the API key or the remote session is gone, so a reconnect cannot help
SEND_TIMEOUT = 10.0
KEPT_ANSWERS = 64
LEVELS = logging.getLevelNamesMapping()


def server_qr(value: object) -> str | None:
    """The QR code text the server sent, or None when it is not text."""
    return value if isinstance(value, str) and value else None


class ServerMessages:
    """What this client does with each message the server starts, whichever adapter brought it."""

    def __init__(self, service: RemoteService) -> None:
        self.service = service
        self.log_seq = 0
        self.recent_logs: deque[str] = deque(maxlen=3)
        self.log_lock = Lock()
        self.cdm_lock = Lock()
        self.cdm_sessions: dict[str, bytes] = {}

    def route(self, route: str) -> str:
        return route.format(session_id=self.service._session_id)

    def show_logs(self, records: list[LogRecord]) -> None:
        """Log the service's server-side records here, each once: a record that the other path brought is skipped."""
        from unshackle.core.api.sanitize import sanitize_log

        with self.log_lock:
            for record in records:
                seq = int(record.get("seq", 0))
                if seq <= self.log_seq:
                    continue
                message = sanitize_log(record.get("message", ""), keep_lines=True)
                self.service.log.log(LEVELS.get(str(record.get("level", "INFO")), logging.INFO), message)
                if qr := server_qr(record.get("qr")):
                    self.service.auth_qr = qr
                    if self.service._input_bridge is None:
                        show_qr(qr)
                self.recent_logs.append(message.strip())
                self.log_seq = seq

    def answer_cdm(self, call: dict[str, Any]) -> dict[str, Any]:
        """Run one of the server's CDM calls on this machine's device; both adapters share its CDM sessions."""
        from unshackle.core.cdm.client_relay import answer_cdm_call

        with self.cdm_lock:
            answer = answer_cdm_call(self.service.local_cdm, call, self.cdm_sessions)
        self.service.log.debug(
            f"Answered a server CDM call: {call.get('op')} ({'refused' if 'error' in answer else 'ok'})"
        )
        return answer

    def ask_user(self, prompt: str, qr: object = None) -> str:
        """Ask the user the server's prompt, with control characters stripped as the server text can hold any."""
        from unshackle.core.api.sanitize import sanitize_log

        prompt = sanitize_log(prompt, keep_lines=True)
        code = server_qr(qr)
        bridge = self.service._input_bridge
        try:
            return str(bridge.request_input(prompt, qr=code) if bridge else prompt_user(prompt, code))
        except EOFError:
            raise RemoteError("Remote auth needs interactive input but stdin is closed", ClientErrorKind.AUTH) from None

    def note_waiting(self, seen: int) -> None:
        """Show the last service log lines while the login waits on the user (a pair code, an approval link)."""
        if self.log_seq == seen and self.recent_logs:
            notice = "\n".join(self.recent_logs)
            if notice != self.service.auth_notice:
                self.service.auth_notice = notice
                show_notice(notice, self.service.auth_qr)

    def close_cdm_sessions(self) -> None:
        """Close the local CDM sessions that the server's calls opened; best effort."""
        cdm = self.service.local_cdm
        with self.cdm_lock:
            for session_id in self.cdm_sessions.values():
                with contextlib.suppress(Exception):
                    cdm.close(session_id)
            self.cdm_sessions.clear()

    def login_done(self) -> None:
        self.close_cdm_sessions()
        if self.service.auth_notice is not None or self.service.auth_qr is not None:
            self.service.auth_notice = self.service.auth_qr = None
            show_notice(None)


class PollingChannel:
    """The fallback: the client asks the server for each message.

    The client uses it when a gateway or the server refuses the channel upgrade, when the connection fails, and while
    the channel is down. It goes together with the polling routes.
    """

    def __init__(self, messages: ServerMessages) -> None:
        self.messages = messages
        self.service = messages.service
        self.keepalive_stop = Event()
        self.keepalive_stop.set()
        self.drain_lock = Lock()
        self.cdm_lock = Lock()
        self.cdm_requests = 0
        self.cdm_stop = Event()
        self.paused: Callable[[], bool] = lambda: False

    def wait_auth(self, timeout: float = AUTH_TIMEOUT, step: Callable[[], bool] | None = None) -> None:
        """Poll the server until authentication completes, and answer its prompts and CDM calls.

        ``step`` replaces :meth:`auth_step` for an adapter that learns the login state another way.
        """
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if (step or self.auth_step)():
                    return
            raise RemoteError("Remote authentication timed out", ClientErrorKind.AUTH)
        finally:
            self.messages.login_done()

    def auth_step(self) -> bool:
        """Poll the login state once: True when authenticated. Answer a prompt or a CDM call, or wait one interval."""
        resp = cast(PromptReply, self.service.client.get(self.messages.route(PROMPT_ROUTE)))
        status = resp.get("status")
        seen = self.messages.log_seq
        self.drain_logs()
        if status == AuthStatus.AUTHENTICATED:
            return True
        if status == AuthStatus.AUTHENTICATING:
            self.messages.note_waiting(seen)
        if status == AuthStatus.FAILED:
            raise RemoteError(
                f"Remote auth failed: {resp.get('error', 'Authentication failed on server')}", ClientErrorKind.AUTH
            )
        if status == AuthStatus.PENDING_INPUT and isinstance(resp.get("cdm_call"), dict):
            self.answer(json.dumps(self.messages.answer_cdm(resp["cdm_call"])))
        elif status == AuthStatus.PENDING_INPUT:
            self.answer(self.messages.ask_user(resp.get("prompt") or "Enter input: ", resp.get("qr")))
        else:
            time.sleep(AUTH_POLL_INTERVAL)
        return False

    def answer(self, response: str) -> None:
        self.service.client.post(self.messages.route(PROMPT_ROUTE), PromptAnswer(response=response))

    def keep_alive(self) -> None:
        """Send keep-alive requests so that a long download or mux operation does not outlive the server's idle TTL.

        ``GET /api/session/{session_id}`` refreshes the idle timer and reports the TTL, so the thread sends a request
        at an interval of one third of it, until :meth:`stop_keepalive`. A client that dies stops sending requests,
        so the server still expires an abandoned remote session.
        """
        if not self.keepalive_stop.is_set():
            return
        stop = self.keepalive_stop = Event()
        endpoint = self.messages.route(SESSION_ROUTE)

        def ping() -> None:
            info = cast(SessionInfo, self.service.client.probe(endpoint))
            interval = max(float(info.get("expires_in") or 300) / 3, KEEPALIVE_MIN_INTERVAL)
            while not stop.wait(interval):
                self.service.client.probe(endpoint)

        Thread(target=ping, name=f"{self.service.service_tag}-keepalive", daemon=True).start()

    def stop_keepalive(self) -> None:
        self.keepalive_stop.set()

    def runs_service_code(self, endpoint: str) -> bool:
        """Whether a request to *endpoint* runs service code on the server, which can make CDM calls."""
        own = {self.messages.route(route) for route in (PROMPT_ROUTE, LOGS_ROUTE, SAMPLES_ROUTE)}
        return endpoint.startswith(self.messages.route(SESSION_ROUTE) + "/") and endpoint.partition("?")[0] not in own

    @contextlib.contextmanager
    def in_flight(self, endpoint: str) -> Iterator[None]:
        """Answer the server's CDM calls while a remote session request runs service code.

        One poller serves every concurrent request, and the local CDM sessions close when the last one ends.
        """
        if not self.runs_service_code(endpoint):
            yield
            return
        with self.cdm_lock:
            self.cdm_requests += 1
            if self.cdm_requests == 1:
                self.cdm_stop = Event()
                Thread(
                    target=self.poll_cdm_calls,
                    args=(self.cdm_stop,),
                    name=f"{self.service.service_tag}-cdm",
                    daemon=True,
                ).start()
        try:
            yield
        finally:
            with self.cdm_lock:
                self.cdm_requests -= 1
                last = self.cdm_requests == 0
                if last:
                    self.cdm_stop.set()
            if last:
                self.messages.close_cdm_sessions()

    def poll_cdm_calls(self, stop: Event, interval: float | None = None) -> None:
        endpoint = self.messages.route(PROMPT_ROUTE)
        while not stop.wait(CDM_POLL_INTERVAL if interval is None else interval):
            if self.paused():
                continue
            try:
                call = cast(PromptReply, self.service.client.probe(endpoint)).get("cdm_call")
                if isinstance(call, dict):
                    with quiet():
                        self.answer(json.dumps(self.messages.answer_cdm(call)))
            except (requests.RequestException, ValueError, SystemExit) as e:
                self.service.log.debug(f"CDM call poll failed: {e}")

    def drain_logs(self) -> None:
        """Fetch the service's server-side log records for this remote session and log them here.

        A relay failure must never break the operation itself, so this method swallows every error.
        """
        if not self.service._session_id:
            return
        with self.drain_lock:
            try:
                with quiet():
                    endpoint = self.messages.route(LOGS_ROUTE) + f"?since={self.messages.log_seq}"
                    resp = cast(LogsReply, self.service.client.get_optional(endpoint))
            except (Exception, SystemExit):
                return
            if resp:
                self.messages.show_logs(resp.get("logs", []))

    def draining(self) -> bool:
        """Whether the server announced a restart."""
        info = cast(SessionInfo, self.service.client.probe(self.messages.route(SESSION_ROUTE)))
        return bool(info.get("draining"))

    def close(self) -> None:
        self.stop_keepalive()


class WebSocketChannel:
    """The remote session channel: the server sends its messages on a WebSocket that a daemon thread holds open.

    While the WebSocket is down, :attr:`fallback` polls and the thread reconnects with a backoff. The thread never
    reopens a session: a close with ``CHANNEL_SESSION_GONE`` ends it, and the next call finds the session gone.
    """

    def __init__(self, messages: ServerMessages, fallback: PollingChannel) -> None:
        self.messages = messages
        self.service = messages.service
        self.fallback = fallback
        self.headers = ws_headers(self.service.client)
        self.connected = Event()
        self.attempted = Event()
        self.changed = Event()
        self.status: str | None = None
        self.error = ""
        self.failure: RemoteError | None = None
        self.draining_now = False
        self.answers: dict[str, ChannelAnswer | None] = {}  # None while this client still works on the ask
        fallback.paused = self.connected.is_set
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task[None] | None = None
        self.thread = Thread(
            target=lambda: asyncio.run(self.run()), name=f"{self.service.service_tag}-channel", daemon=True
        )

    def open(self, timeout: float = CONNECT_TIMEOUT) -> bool:
        """Connect once; False when the server or a gateway refuses the channel, or the connection fails."""
        self.thread.start()
        self.attempted.wait(timeout)
        if not self.connected.is_set():
            self.close()
            return False
        return True

    async def run(self) -> None:
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        try:
            await self.connect()
        except asyncio.CancelledError:
            pass
        finally:
            self.attempted.set()
            self.changed.set()

    async def connect(self) -> None:
        backoff = RECONNECT_DELAY
        async with aiohttp.ClientSession(headers=self.headers) as http:
            while True:
                code = None
                url = (
                    self.service.client.server_url
                    + self.messages.route(CHANNEL_ROUTE)
                    + f"?since={self.messages.log_seq}"
                )
                try:
                    async with http.ws_connect(url, heartbeat=HEARTBEAT) as ws:
                        self.ws = ws
                        self.connected.set()
                        self.attempted.set()
                        self.fallback.stop_keepalive()
                        backoff = RECONNECT_DELAY
                        await self.read(ws)
                        code = ws.close_code
                except aiohttp.WSServerHandshakeError as e:
                    log.debug(f"The server refused the session channel: HTTP {e.status}")
                    if not self.attempted.is_set() or e.status in GONE_STATUSES:
                        return
                    backoff = max(backoff, retry_after(e) or 0)
                except (TimeoutError, aiohttp.ClientError, OSError) as e:
                    log.debug(f"The session channel could not connect: {e!r}")
                    if not self.attempted.is_set():
                        return
                finally:
                    self.ws = None
                    self.connected.clear()
                    self.changed.set()
                if code == CHANNEL_SESSION_GONE:
                    log.debug("The server closed the session channel: the remote session is gone")
                    return
                self.fallback.keep_alive()
                log.debug(f"The session channel dropped, reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if isinstance(data, dict):
                await self.receive(ws, data)

    async def receive(self, ws: aiohttp.ClientWebSocketResponse, data: dict[str, Any]) -> None:
        kind = data.get("type")
        if kind == ChannelMessage.LOG:
            self.messages.show_logs([cast(LogRecord, data)])
        elif kind == ChannelMessage.STATUS:
            self.status, self.error = data.get("status"), str(data.get("error") or "")
            self.changed.set()
        elif kind == ChannelMessage.DRAIN:
            self.draining_now = bool(data.get("draining"))
        elif kind in (ChannelMessage.PROMPT, ChannelMessage.CDM_CALL) and isinstance(data.get("id"), str):
            ask_id = data["id"]
            if ask_id not in self.answers:
                self.answers[ask_id] = None
                if len(self.answers) > KEPT_ANSWERS:
                    self.answers.pop(next(iter(self.answers)))
                Thread(target=self.answer, args=(data,), name=f"{self.service.service_tag}-ask", daemon=True).start()
            elif kept := self.answers[ask_id]:
                await ws.send_json(kept)  # the server sends an ask again after a reconnect when its answer was lost

    def answer(self, data: dict[str, Any]) -> None:
        """Answer one prompt or CDM call on this thread, so the channel keeps reading while the user types."""
        cdm_call = data["type"] == ChannelMessage.CDM_CALL
        answer = ChannelAnswer(type=ChannelMessage.ANSWER, id=data["id"], response="")
        try:
            if cdm_call:
                answer["response"] = json.dumps(self.messages.answer_cdm(data.get("call") or {}))
            else:
                answer["response"] = self.messages.ask_user(str(data.get("prompt") or "Enter input: "), data.get("qr"))
        except (Exception, RemoteError) as e:  # an unanswered ask would hold the service thread until it times out
            if isinstance(e, RemoteError):
                self.failure = e
                self.changed.set()
            log.debug(f"Could not answer the server's {data['type']}: {e!r}")
            if cdm_call:
                answer["response"] = json.dumps({"error": str(e) or type(e).__name__})
            else:
                answer["error"] = str(e) or type(e).__name__
        self.answers[data["id"]] = answer
        loop, ws = self.loop, self.ws
        if loop is not None and ws is not None:
            with contextlib.suppress(Exception):  # a lost answer goes again when the server sends the ask again
                asyncio.run_coroutine_threadsafe(ws.send_json(answer), loop).result(SEND_TIMEOUT)

    def wait_auth(self, timeout: float = AUTH_TIMEOUT) -> None:
        """Wait for the login state on the channel, and poll while the channel is down."""
        self.fallback.wait_auth(timeout, self.auth_step)

    def auth_step(self) -> bool:
        """Wait one interval for the login state: True when authenticated."""
        if self.failure is not None:
            raise self.failure
        if not self.connected.is_set():
            return self.fallback.auth_step()
        self.changed.clear()
        if self.status == AuthStatus.AUTHENTICATED:
            return True
        if self.status == AuthStatus.FAILED:
            raise RemoteError(
                f"Remote auth failed: {self.error or 'Authentication failed on server'}", ClientErrorKind.AUTH
            )
        seen = self.messages.log_seq
        if not self.changed.wait(AUTH_POLL_INTERVAL) and self.status == AuthStatus.AUTHENTICATING:
            self.messages.note_waiting(seen)
        return False

    def keep_alive(self) -> None:
        """An open channel keeps the remote session alive; the fallback pings while the channel is down."""
        if not self.connected.is_set():
            self.fallback.keep_alive()

    def in_flight(self, endpoint: str) -> AbstractContextManager[None]:
        """The fallback's CDM call poller, which polls only while the channel is down."""
        return self.fallback.in_flight(endpoint)

    def drain_logs(self) -> None:
        if not self.connected.is_set():
            self.fallback.drain_logs()

    def draining(self) -> bool:
        return self.draining_now if self.connected.is_set() else self.fallback.draining()

    def close(self) -> None:
        if self.loop and self.task and self.thread.is_alive():
            with contextlib.suppress(RuntimeError):  # the loop closes before its thread ends
                self.loop.call_soon_threadsafe(self.task.cancel)
            self.thread.join(5)
        self.fallback.close()
        self.messages.close_cdm_sessions()


Channel = PollingChannel | WebSocketChannel


def open_channel(service: RemoteService) -> Channel:
    """The remote session channel when the server takes it, else the polling fallback, also for every later session."""
    messages = ServerMessages(service)
    fallback = PollingChannel(messages)
    if service.client.no_channel:
        return fallback
    channel = WebSocketChannel(messages, fallback)
    if channel.open():
        return channel
    service.client.no_channel = True
    return fallback
