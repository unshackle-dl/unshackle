"""Thread-safe bridge for interactive input during remote authentication.

When a service calls ``request_input()`` during ``authenticate()`` on the
server, the InputBridge pauses the auth thread and exposes the prompt to
the HTTP layer so a remote client can poll for it, collect the user's
response, and submit it back.  The auth thread then resumes with the
response value.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AuthStatus(Enum):
    """Authentication lifecycle states for a remote session."""

    AUTHENTICATING = "authenticating"
    PENDING_INPUT = "pending_input"
    AUTHENTICATED = "authenticated"
    FAILED = "failed"


class BridgeCancelledError(Exception):
    """Raised when the bridge is cancelled (client disconnected, session deleted)."""


@dataclass
class InputBridge:
    """Thread-safe bridge between a sync auth thread and the async HTTP layer.

    The auth thread calls :meth:`request_input` which blocks until the
    remote client submits a response via the HTTP prompt endpoints.
    """

    _prompt: Optional[str] = field(default=None, init=False, repr=False)
    _response: Optional[str] = field(default=None, init=False, repr=False)
    _status: AuthStatus = field(default=AuthStatus.AUTHENTICATING, init=False)
    _error: Optional[str] = field(default=None, init=False, repr=False)
    _cancelled: bool = field(default=False, init=False, repr=False)
    _response_ready: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def request_input(self, prompt: str, timeout: float = 600.0) -> str:
        """Block until the remote client submits a response for *prompt*.

        Args:
            prompt: The message to display to the remote user.
            timeout: Maximum seconds to wait for a response.

        Returns:
            The string response from the remote client.

        Raises:
            TimeoutError: If no response is received within *timeout*.
            BridgeCancelledError: If the bridge was cancelled.
        """
        with self._lock:
            if self._cancelled:
                raise BridgeCancelledError("Session was cancelled")
            self._prompt = prompt
            self._response = None
            self._status = AuthStatus.PENDING_INPUT
            self._response_ready.clear()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._response_ready.wait(timeout=0.5):
                break
            with self._lock:
                if self._cancelled:
                    raise BridgeCancelledError("Session was cancelled")
        else:
            with self._lock:
                self._status = AuthStatus.FAILED
                self._error = "Input request timed out waiting for client response"
            raise TimeoutError(f"No client response for prompt within {timeout}s")

        with self._lock:
            if self._cancelled:
                raise BridgeCancelledError("Session was cancelled")
            response = self._response or ""
            self._prompt = None
            self._response = None
            self._status = AuthStatus.AUTHENTICATING
            return response

    def get_pending_prompt(self) -> Optional[str]:
        """Return the current prompt if the auth thread is waiting for input."""
        with self._lock:
            if self._status == AuthStatus.PENDING_INPUT:
                return self._prompt
            return None

    def submit_response(self, response: str) -> bool:
        """Deliver the client's response and unblock the auth thread.

        Returns:
            ``True`` if a prompt was pending and the response was accepted,
            ``False`` otherwise.
        """
        with self._lock:
            if self._status != AuthStatus.PENDING_INPUT:
                return False
            self._response = response
        self._response_ready.set()
        return True

    def cancel(self) -> None:
        """Cancel the bridge, unblocking any waiting auth thread."""
        with self._lock:
            self._cancelled = True
            self._status = AuthStatus.FAILED
            self._error = "Session cancelled"
        self._response_ready.set()

    @property
    def status(self) -> AuthStatus:
        with self._lock:
            return self._status

    @status.setter
    def status(self, value: AuthStatus) -> None:
        with self._lock:
            self._status = value

    @property
    def error(self) -> Optional[str]:
        with self._lock:
            return self._error

    @error.setter
    def error(self, value: Optional[str]) -> None:
        with self._lock:
            self._error = value
