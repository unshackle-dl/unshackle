"""Thread-safe bridge for interactive input during remote authentication.

When a service calls ``request_input()`` during ``authenticate()`` on the
server, the InputBridge pauses the auth thread and exposes the prompt to
the HTTP layer so a remote client can poll for it, collect the user's
response, and submit it back.  The auth thread then resumes with the
response value.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class AuthStatus(Enum):
    """Authentication lifecycle states for a remote session."""

    AUTHENTICATING = "authenticating"
    PENDING_INPUT = "pending_input"
    AUTHENTICATED = "authenticated"
    FAILED = "failed"


# Seconds the server waits for a remote client to answer an input prompt before
# giving up; also the TTL SessionStore grants AUTHENTICATING/PENDING_INPUT sessions.
AUTH_INPUT_TIMEOUT = 600.0
CDM_CALL_TIMEOUT = 60.0
# A listening client polls every few seconds, so a CDM call nobody fetches in this time has no client behind it.
CDM_PICKUP_TIMEOUT = 10.0

CDM_CALL_FALLBACK_PROMPT = (
    "This service needs your local CDM during login, and your unshackle cannot answer it. Update unshackle"
)


@dataclass
class InputBridge:
    """Thread-safe bridge between a sync auth thread and the async HTTP layer.

    The auth thread calls :meth:`request_input` which blocks until the
    remote client submits a response through the HTTP prompt endpoints.
    """

    _prompt: Optional[str] = field(default=None, init=False, repr=False)
    _response: Optional[str] = field(default=None, init=False, repr=False)
    _status: AuthStatus = field(default=AuthStatus.AUTHENTICATING, init=False)
    _cancelled: bool = field(default=False, init=False, repr=False)
    _answered: bool = field(default=False, init=False, repr=False)
    _cdm_call: Optional[dict[str, Any]] = field(default=None, init=False, repr=False)
    _response_ready: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _picked_up: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def request_input(self, prompt: str, timeout: float = AUTH_INPUT_TIMEOUT) -> str:
        """Block until the remote client submits a response for *prompt*.

        Args:
            prompt: The message to display to the remote user.
            timeout: Maximum seconds to wait for a response.

        Returns:
            The string response from the remote client.

        Raises:
            TimeoutError: If the bridge gets no response within *timeout*.
            RuntimeError: If the server cancelled the bridge, or authentication is complete.
        """
        with self._lock:
            if self._cancelled:
                raise RuntimeError("Session was cancelled")
            if self._status == AuthStatus.AUTHENTICATED:
                raise RuntimeError(f"Remote mode cannot relay a prompt after authentication: {prompt}")
            self._prompt = prompt
            self._response = None
            self._status = AuthStatus.PENDING_INPUT
            self._response_ready.clear()

        # cancel() and submit_response() both .set() the event, so a single wait
        # returns on signal-or-cancel; no poll loop needed.
        if not self._response_ready.wait(timeout=timeout):
            with self._lock:
                self._status = AuthStatus.FAILED
            raise TimeoutError(f"No client response for prompt within {timeout}s")

        with self._lock:
            if self._cancelled:
                raise RuntimeError("Session was cancelled")
            response = self._response or ""
            self._prompt = None
            self._response = None
            self._status = AuthStatus.AUTHENTICATING
            return response

    def request_cdm(
        self, call: dict[str, Any], timeout: float = CDM_CALL_TIMEOUT, pickup: float = CDM_PICKUP_TIMEOUT
    ) -> dict[str, Any]:
        """Block until the remote client runs *call* on its own CDM, and return its JSON answer.

        Unlike a prompt, a CDM call also works after authentication, and it leaves the auth status as it was.
        A call that no client fetches within ``pickup`` seconds fails at once, so a vanished client holds the
        service thread for seconds, not for the full ``timeout``.
        """
        with self._lock:
            if self._cancelled:
                raise RuntimeError("Session was cancelled")
            previous = self._status
            self._prompt = CDM_CALL_FALLBACK_PROMPT
            self._cdm_call = call
            self._response = None
            self._status = AuthStatus.PENDING_INPUT
            self._response_ready.clear()
            self._picked_up.clear()

        picked_up = self._picked_up.wait(timeout=pickup)
        answered = picked_up and self._response_ready.wait(timeout=timeout)

        with self._lock:
            answer = self._response
            self._prompt = self._response = self._cdm_call = None
            cancelled = self._cancelled
            if not cancelled:
                self._status = previous
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

    def get_pending_cdm_call(self) -> Optional[dict[str, Any]]:
        """Return the CDM call that service code waits on, if any, and mark it as fetched by a client."""
        with self._lock:
            if self._awaiting_answer() and self._cdm_call is not None:
                self._picked_up.set()
                return self._cdm_call
            return None

    def get_pending_prompt(self) -> Optional[str]:
        """Return the current prompt when the auth thread waits for input."""
        with self._lock:
            if self._awaiting_answer():
                return self._prompt
            return None

    def submit_response(self, response: str) -> bool:
        """Deliver the client's response and unblock the auth thread.

        Returns:
            ``True`` if a prompt was pending and the bridge accepted the response,
            ``False`` otherwise.
        """
        with self._lock:
            if not self._awaiting_answer():
                return False
            self._response = response
            if self._cdm_call is None:
                self._answered = True
        self._picked_up.set()
        self._response_ready.set()
        return True

    def _awaiting_answer(self) -> bool:
        """True while a prompt or CDM call waits and no answer has arrived; call it under the lock.

        An answered call stops being pending at once, before the waiting thread wakes, so a client
        that polls again straight away never sees it twice.
        """
        return self._status == AuthStatus.PENDING_INPUT and not self._response_ready.is_set()

    def cancel(self) -> None:
        """Cancel the bridge, unblocking any waiting auth thread."""
        with self._lock:
            self._cancelled = True
            self._status = AuthStatus.FAILED
        self._picked_up.set()
        self._response_ready.set()

    @property
    def answered(self) -> bool:
        """``True`` once the bridge has accepted a response from the client."""
        with self._lock:
            return self._answered

    @property
    def status(self) -> AuthStatus:
        with self._lock:
            return self._status

    @status.setter
    def status(self, value: AuthStatus) -> None:
        with self._lock:
            self._status = value
