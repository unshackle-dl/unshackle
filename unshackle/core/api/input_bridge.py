"""Thread-safe bridge for the prompts and CDM calls that service code on the server sends to the remote client.

When a service calls ``request_input()`` or its client-device CDM makes a call, the InputBridge blocks the
service thread on an *ask* until the remote client answers it. The remote session channel carries an ask when one is
attached; otherwise the prompt routes serve it, one at a time. Each ask belongs to the path that carried it
first, and only that path can answer it.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from unshackle.core.remote_protocol import AuthStatus

if TYPE_CHECKING:
    from unshackle.core.api.session_channel import SessionChannel

# Seconds the server waits for a remote client to answer an input prompt before
# giving up; also the TTL SessionStore grants AUTHENTICATING sessions.
AUTH_INPUT_TIMEOUT = 600.0
CDM_CALL_TIMEOUT = 60.0
# A listening client polls every few seconds, so a CDM call nobody fetches in this time has no client behind it.
CDM_PICKUP_TIMEOUT = 10.0

CDM_CALL_FALLBACK_PROMPT = (
    "This service needs your local CDM during login, and your unshackle cannot answer it. Update unshackle"
)

CHANNEL = "channel"
POLL = "poll"


@dataclass(eq=False)
class Ask:
    """One prompt or CDM call that service code waits on."""

    id: str
    prompt: str
    cdm_call: Optional[dict[str, Any]] = None
    owner: Optional[str] = None  # CHANNEL or POLL: the path that carried the ask, and the only one that answers it
    response: Optional[str] = None
    error: Optional[str] = None  # the client could not answer, and says why
    picked_up: threading.Event = field(default_factory=threading.Event)
    answered: threading.Event = field(default_factory=threading.Event)


@dataclass
class InputBridge:
    """Thread-safe bridge between the service threads and the async HTTP layer.

    A service thread calls :meth:`request_input` or :meth:`request_cdm`, which block until the remote client
    answers through the remote session channel or the prompt routes.
    """

    channel: Optional[SessionChannel] = field(default=None, init=False, repr=False)
    _asks: dict[str, Ask] = field(default_factory=dict, init=False, repr=False)
    _status: AuthStatus = field(default=AuthStatus.AUTHENTICATING, init=False)
    _cancelled: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _open(self, prompt: str, cdm_call: Optional[dict[str, Any]] = None) -> Ask:
        """Register an ask and offer it to the attached channel; call it with the lock held."""
        if self._cancelled:
            raise RuntimeError("Session was cancelled")
        ask = Ask(uuid.uuid4().hex, prompt, cdm_call)
        if self.channel is not None and self.channel.offer(ask):
            ask.owner = CHANNEL
            ask.picked_up.set()
        self._asks[ask.id] = ask
        return ask

    def request_input(self, prompt: str, timeout: float = AUTH_INPUT_TIMEOUT) -> str:
        """Block until the remote client submits a response for *prompt*.

        After authentication only an attached session channel can carry a prompt, as a polling client stops
        asking for prompts once it is logged in.

        Raises:
            TimeoutError: If the bridge gets no response within *timeout*.
            RuntimeError: If the server cancelled the bridge, or authentication is complete and no channel is attached.
        """
        with self._lock:
            if self._status == AuthStatus.AUTHENTICATED and not (self.channel and self.channel.attached):
                if self._cancelled:
                    raise RuntimeError("Session was cancelled")
                raise RuntimeError(f"Remote mode cannot relay a prompt after authentication: {prompt}")
            ask = self._open(prompt)
        try:
            # cancel() and an answer both set the event, so a single wait returns on signal-or-cancel
            if not ask.answered.wait(timeout=timeout):
                raise TimeoutError(f"No client response for prompt within {timeout}s")
            with self._lock:
                if self._cancelled:
                    raise RuntimeError("Session was cancelled")
                if ask.error is not None:
                    raise RuntimeError(f"The client could not answer the prompt: {ask.error}")
                return ask.response or ""
        finally:
            with self._lock:
                self._asks.pop(ask.id, None)

    def request_cdm(
        self, call: dict[str, Any], timeout: float = CDM_CALL_TIMEOUT, pickup: float = CDM_PICKUP_TIMEOUT
    ) -> dict[str, Any]:
        """Block until the remote client runs *call* on its own CDM, and return its JSON answer.

        Unlike a prompt, a CDM call also works after authentication, and it leaves the auth status as it was.
        A call that no client fetches within ``pickup`` seconds fails at once, so a vanished client holds the
        service thread for seconds, not for the full ``timeout``.
        """
        with self._lock:
            ask = self._open(CDM_CALL_FALLBACK_PROMPT, call)
        try:
            picked_up = ask.picked_up.wait(timeout=pickup)
            answered = picked_up and ask.answered.wait(timeout=timeout)
            with self._lock:
                answer = ask.response
                cancelled = self._cancelled
        finally:
            with self._lock:
                self._asks.pop(ask.id, None)
        if cancelled:
            raise RuntimeError("Session was cancelled")
        if not picked_up:
            raise TimeoutError(f"No client fetched the CDM call within {pickup:.0f}s, so none is listening")
        if not answered:
            raise TimeoutError(f"The client did not answer the CDM call within {timeout:.0f}s")
        try:
            result = json.loads(answer or "")
        except ValueError:
            result = None
        if not isinstance(result, dict):
            raise RuntimeError("The client did not answer the CDM call; update unshackle on the client")
        if result.get("error"):
            raise RuntimeError(f"The client's CDM refused the call: {result['error']}")
        return result

    def _pollable(self) -> list[Ask]:
        """The asks the prompt routes can serve: unanswered, and not carried by the channel; call it under the lock.

        An answered ask stops being pending at once, before the waiting thread wakes, so a client that polls again
        straight away never sees it twice.
        """
        return [a for a in self._asks.values() if not a.answered.is_set() and a.owner != CHANNEL]

    def get_pending_cdm_call(self) -> Optional[dict[str, Any]]:
        """Return the CDM call that service code waits on, if any, and mark it as fetched by a client."""
        with self._lock:
            ask = next((a for a in self._pollable() if a.cdm_call is not None), None)
            if ask is None:
                return None
            ask.owner = POLL
            ask.picked_up.set()
            return ask.cdm_call

    def get_pending_prompt(self) -> Optional[str]:
        """Return the prompt of the first ask the prompt routes can serve, and keep that ask on them."""
        with self._lock:
            ask = next(iter(self._pollable()), None)
            if ask is None:
                return None
            ask.owner = POLL
            return ask.prompt

    def submit_response(self, response: str) -> bool:
        """Answer the oldest ask that the prompt routes served, else the oldest one they can serve.

        Returns:
            ``True`` if an ask was pending and the bridge accepted the response, ``False`` otherwise.
        """
        with self._lock:
            pending = self._pollable()
            ask = next((a for a in pending if a.owner == POLL), pending[0] if pending else None)
            if ask is None:
                return False
            self._answer(ask, response)
        return True

    def answer(self, ask_id: str, response: str, error: Optional[str] = None) -> bool:
        """Answer the ask with this id that the remote session channel carried; False when no such ask waits."""
        with self._lock:
            ask = self._asks.get(ask_id)
            if ask is None or ask.owner != CHANNEL or ask.answered.is_set():
                return False
            ask.error = error
            self._answer(ask, response)
        return True

    @staticmethod
    def _answer(ask: Ask, response: str) -> None:
        ask.response = response
        ask.picked_up.set()
        ask.answered.set()

    def claim_for_channel(self) -> list[Ask]:
        """Give every unanswered ask that the prompt routes did not serve to a newly attached session channel."""
        with self._lock:
            asks = [a for a in self._asks.values() if not a.answered.is_set() and a.owner != POLL]
            for ask in asks:
                ask.owner = CHANNEL
                ask.picked_up.set()
            return asks

    def release_channel_asks(self) -> None:
        """Hand the unanswered asks of a detached channel back to the prompt routes, so none waits on a dead socket.

        A prompt after login stays with the channel, as a polling client asks for prompts only while it logs in.
        """
        with self._lock:
            for ask in self._asks.values():
                pollable = ask.cdm_call is not None or self._status != AuthStatus.AUTHENTICATED
                if ask.owner == CHANNEL and pollable and not ask.answered.is_set():
                    ask.owner = None

    def cancel(self) -> None:
        """Cancel the bridge, unblocking any waiting service thread."""
        with self._lock:
            self._cancelled = True
            asks = list(self._asks.values())
        for ask in asks:
            ask.picked_up.set()
            ask.answered.set()

    def ensure_active(self) -> None:
        """Raise RuntimeError once the server cancelled the bridge, so a login loop stops."""
        with self._lock:
            if self._cancelled:
                raise RuntimeError("Session was cancelled")

    @property
    def status(self) -> AuthStatus:
        """``pending_input`` while an ask waits for its answer, else the login state."""
        with self._lock:
            if self._status != AuthStatus.FAILED and any(not a.answered.is_set() for a in self._asks.values()):
                return AuthStatus.PENDING_INPUT
            return self._status

    @property
    def login_status(self) -> AuthStatus:
        """The login state alone: a waiting ask does not make it ``pending_input``."""
        with self._lock:
            return self._status

    @login_status.setter
    def login_status(self, value: AuthStatus) -> None:
        with self._lock:
            self._status = value


NO_USER_MESSAGE = "This login needs the user, and only a download job or a remote session can reach the user"


class NoUserBridge(InputBridge):
    """Bridge for a request with no channel to the user: a login step that needs the user fails at once."""

    def request_input(self, prompt: str, timeout: float = AUTH_INPUT_TIMEOUT) -> str:
        raise RuntimeError(NO_USER_MESSAGE)

    def ensure_active(self) -> None:
        raise RuntimeError(NO_USER_MESSAGE)
