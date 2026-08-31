"""Session log forwarding: buffer semantics, mirror wrapping, handler drain."""

import logging
import threading

import pytest

from unshackle.core.api.handlers import session_logs_handler
from unshackle.core.api.session_log import SessionLogBuffer, SessionLogMirror, capture_service_logs
from unshackle.core.api.session_store import get_session_store


def test_buffer_sequences_and_since() -> None:
    buf = SessionLogBuffer(maxlen=3)
    for i in range(5):
        buf.append(logging.INFO, f"msg {i}")
    records = buf.since(0)
    # bounded: only the newest 3 survive, sequence numbers keep counting
    assert [r["seq"] for r in records] == [3, 4, 5]
    assert buf.since(4) == [records[-1]]
    assert buf.last_seq == 5


def test_buffer_sanitizes_messages() -> None:
    buf = SessionLogBuffer()
    buf.append(logging.WARNING, "line1\nline2")
    record = buf.since(0)[0]
    assert "\n" not in record["message"]
    assert record["level"] == "WARNING"


def test_mirror_copies_all_levels_and_lazy_formatting() -> None:
    buf = SessionLogBuffer()
    mirror = SessionLogMirror(logging.getLogger("test-session-log"), buf)
    mirror.info("hello %s", "world")
    mirror.warning("warn")
    mirror.error("bad")
    messages = [(r["level"], r["message"]) for r in buf.since(0)]
    assert messages == [("INFO", "hello world"), ("WARNING", "warn"), ("ERROR", "bad")]


def test_mirror_buffers_below_logger_level() -> None:
    logger = logging.getLogger("test-session-log-level")
    logger.setLevel(logging.WARNING)
    buf = SessionLogBuffer()
    SessionLogMirror(logger, buf).info("still captured")
    assert [r["message"] for r in buf.since(0)] == ["still captured"]


def test_capture_service_logs_attaches_and_detaches() -> None:
    logger = logging.getLogger("test-capture-tag")
    buf = SessionLogBuffer()
    with capture_service_logs("test-capture-tag", buf):
        logger.warning("inside")
    logger.warning("outside")
    assert [r["message"] for r in buf.since(0)] == ["inside"]
    assert not logger.handlers


@pytest.mark.asyncio
async def test_session_logs_handler_drains_since() -> None:
    store = get_session_store()
    entry = await store.create("EXAMPLE", object(), session_id="log-test-session")
    try:
        entry.log_buffer = SessionLogBuffer()
        entry.log_buffer.append(logging.INFO, "first")
        entry.log_buffer.append(logging.ERROR, "second")

        resp = await session_logs_handler("log-test-session", since=0)
        import json

        body = json.loads(resp.body)
        assert [r["message"] for r in body["logs"]] == ["first", "second"]
        assert body["last_seq"] == 2

        resp = await session_logs_handler("log-test-session", since=2)
        body = json.loads(resp.body)
        assert body["logs"] == []
        assert body["last_seq"] == 2
    finally:
        await store.delete("log-test-session")


@pytest.mark.asyncio
async def test_session_logs_handler_no_buffer() -> None:
    store = get_session_store()
    await store.create("EXAMPLE", object(), session_id="log-test-nobuf")
    try:
        resp = await session_logs_handler("log-test-nobuf", since=0)
        import json

        assert json.loads(resp.body)["logs"] == []
    finally:
        await store.delete("log-test-nobuf")


def test_capture_service_logs_none_buffer_noop() -> None:
    logger = logging.getLogger("test-capture-none")
    with capture_service_logs("test-capture-none", None):
        logger.warning("nothing captured")
    assert not logger.handlers


def test_capture_service_logs_lowers_level_for_window() -> None:
    logger = logging.getLogger("test-capture-level")
    logger.setLevel(logging.WARNING)
    buf = SessionLogBuffer()
    with capture_service_logs("test-capture-level", buf):
        assert logger.getEffectiveLevel() == logging.INFO
        logger.info("info inside window")
    assert logger.level == logging.WARNING
    assert [r["message"] for r in buf.since(0)] == ["info inside window"]


def test_transport_keys_block_profile_and_service_params() -> None:
    from unshackle.core.api.handlers import LIST_HANDLER_TRANSPORT_KEYS, SESSION_TRANSPORT_KEYS

    assert "profile" in SESSION_TRANSPORT_KEYS
    assert "service_params" in SESSION_TRANSPORT_KEYS
    assert "profile" in LIST_HANDLER_TRANSPORT_KEYS
    assert "service_params" in LIST_HANDLER_TRANSPORT_KEYS


def test_drain_server_logs_survives_client_systemexit() -> None:
    """RemoteClient raises SystemExit on transport errors; the drain must swallow it."""
    from unshackle.core.remote_service import RemoteService

    svc = RemoteService.__new__(RemoteService)
    svc._session_id = "abc"
    svc._log_seq = 0
    svc._log_drain_lock = threading.Lock()

    class ExitingClient:
        def get_optional(self, endpoint: str) -> dict:
            raise SystemExit(1)

    svc.client = ExitingClient()
    svc.drain_server_logs()  # must not raise
    assert svc._log_seq == 0


def test_drain_server_logs_reemits_and_advances_seq() -> None:
    from unshackle.core.remote_service import RemoteService

    svc = RemoteService.__new__(RemoteService)
    svc._session_id = "abc"
    svc._log_seq = 0
    svc._log_drain_lock = threading.Lock()
    svc.log = logging.getLogger("test-drain-reemit")

    class StubClient:
        def get_optional(self, endpoint: str) -> dict:
            assert endpoint.endswith("since=0")
            return {"logs": [{"seq": 1, "level": "WARNING", "message": "server says"}], "last_seq": 1}

    svc.client = StubClient()
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    svc.log.addHandler(handler)
    try:
        svc.drain_server_logs()
    finally:
        svc.log.removeHandler(handler)
    assert svc._log_seq == 1
    assert [(r.levelno, r.getMessage()) for r in records] == [(logging.WARNING, "server says")]


def test_mirror_buffers_exception_cause() -> None:
    buf = SessionLogBuffer()
    mirror = SessionLogMirror(logging.getLogger("test-mirror-exc"), buf)
    try:
        raise ValueError("boom")
    except ValueError:
        mirror.exception("step failed")
    record = buf.since(0)[0]
    assert record["level"] == "ERROR"
    assert record["message"] == "step failed | ValueError: boom"
