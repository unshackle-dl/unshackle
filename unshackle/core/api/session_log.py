"""Per-session capture of service log output for remote-dl clients.

A service running under ``serve`` logs to the server's console, so a remote
client never sees why an auth or manifest step failed. Each remote session
gets a bounded :class:`SessionLogBuffer`; the service instance's ``self.log``
is wrapped in a :class:`SessionLogMirror` that copies every call into the
buffer, and the client drains it through ``GET /api/session/{id}/logs``.

Only ``self.log`` calls are mirrored. Output from module-level loggers (the
manifest parsers, the downloader) stays server-side.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
from collections import deque
from typing import Any, Deque, Dict, List, MutableMapping, Optional, Tuple

from unshackle.core.api.sanitize import sanitize_log

MAX_RECORDS = 500
MAX_MESSAGE_LEN = 2000


class SessionLogBuffer:
    """Thread-safe bounded buffer of log records with a monotonic sequence."""

    def __init__(self, maxlen: int = MAX_RECORDS) -> None:
        self._records: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    def append(self, level: int, message: str) -> None:
        with self._lock:
            self._seq += 1
            self._records.append(
                {
                    "seq": self._seq,
                    "level": logging.getLevelName(level),
                    "message": sanitize_log(message)[:MAX_MESSAGE_LEN],
                    "ts": time.time(),
                }
            )

    def since(self, seq: int) -> List[Dict[str, Any]]:
        """Records with a sequence number greater than *seq*, oldest first."""
        with self._lock:
            return [dict(r) for r in self._records if r["seq"] > seq]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq


def format_message(msg: Any, args: Tuple[Any, ...]) -> str:
    """Render a logging call's message the way ``LogRecord`` would."""
    try:
        return str(msg) % args if args else str(msg)
    except (TypeError, ValueError):
        return str(msg)


class SessionLogMirror(logging.LoggerAdapter):
    """Wraps a service's ``self.log`` so every call also lands in the remote session's buffer.

    The mirror fills the buffer regardless of the server's log level, so a
    client still gets INFO detail from a server that only prints warnings.
    """

    def __init__(self, logger: logging.Logger, buffer: SessionLogBuffer) -> None:
        super().__init__(logger, {})
        self.buffer = buffer

    def log(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        message = format_message(msg, args)
        # self.log.exception(...) carries its cause in exc_info; buffer the
        # exception line only - a full traceback would ship server paths.
        exc_info = kwargs.get("exc_info")
        if exc_info:
            if exc_info is True:
                exc_info = sys.exc_info()
            elif isinstance(exc_info, BaseException):
                exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
            if isinstance(exc_info, tuple) and exc_info[1] is not None:
                cause = "".join(traceback.format_exception_only(exc_info[0], exc_info[1])).strip()
                message = f"{message} | {cause}"
        self.buffer.append(level, message)
        super().log(level, msg, *args, **kwargs)

    def process(self, msg: Any, kwargs: MutableMapping[str, Any]) -> tuple[Any, MutableMapping[str, Any]]:
        return msg, kwargs


class SessionLogHandler(logging.Handler):
    """Copies records from a real logger into a remote session's buffer.

    Attached to the service tag's logger only for the short window before the
    instance's ``self.log`` can be swapped for a :class:`SessionLogMirror`
    (service ``__init__``: geofence checks, proxy selection).
    """

    def __init__(self, buffer: SessionLogBuffer) -> None:
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(record.levelno, record.getMessage())
        except Exception:
            pass


class capture_service_logs:
    """Context manager: mirror a class logger's records into *buffer* while active.

    *logger_name* must be the service CLASS name (``Service.__init__`` logs to
    ``logging.getLogger(self.__class__.__name__)``), not the directory tag.
    The context manager lowers the logger's level to INFO for the window so a
    quiet server still fills the buffer; a no-op when *buffer* is None.

    The logger is process-global, but the capture window only wraps the
    synchronous ``create_service_instance`` call on the event loop thread, so
    two session creates cannot interleave inside it. If an ``await`` ever moves
    inside the window, add a thread/context filter or records cross tenants.
    """

    def __init__(self, logger_name: str, buffer: Optional[SessionLogBuffer]) -> None:
        self._logger = logging.getLogger(logger_name)
        self._handler: Optional[SessionLogHandler] = SessionLogHandler(buffer) if buffer else None
        self._prev_level: Optional[int] = None

    def __enter__(self) -> None:
        if self._handler:
            if self._logger.getEffectiveLevel() > logging.INFO:
                self._prev_level = self._logger.level
                self._logger.setLevel(logging.INFO)
            self._logger.addHandler(self._handler)

    def __exit__(self, *exc: Any) -> None:
        if self._handler:
            self._logger.removeHandler(self._handler)
            if self._prev_level is not None:
                self._logger.setLevel(self._prev_level)
                self._prev_level = None
            self._handler = None
