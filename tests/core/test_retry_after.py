"""RnetSession.get_sleep_time must survive hostile or malformed Retry-After headers."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from unshackle.core.session import RnetSession


@pytest.fixture
def session():
    return RnetSession()


def response(retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return SimpleNamespace(headers=headers)


def test_numeric_retry_after_passes_through(session):
    assert session.get_sleep_time(response("3"), 1) == 3.0


def test_huge_numeric_retry_after_is_clamped(session):
    assert session.get_sleep_time(response("86400"), 1) == session.max_backoff


def test_huge_numeric_retry_after_respects_session_cap():
    session = RnetSession(max_backoff=5.0)
    assert session.get_sleep_time(response("86400"), 1) == 5.0


def test_http_date_retry_after_honoured(session):
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10), usegmt=True)
    assert 5 <= session.get_sleep_time(response(when), 1) <= 10


def test_naive_http_date_does_not_raise(session):
    # "-0000" makes parsedate_to_datetime return a naive datetime
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10)).replace("+0000", "-0000")
    assert 5 <= session.get_sleep_time(response(when), 1) <= 10


def test_past_http_date_yields_no_sleep(session):
    when = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=30), usegmt=True)
    assert session.get_sleep_time(response(when), 1) <= 0


def test_malformed_retry_after_falls_back_to_backoff(session):
    value = session.get_sleep_time(response("not-a-date"), 1)
    assert 0 < value <= session.max_backoff


def test_empty_retry_after_falls_back_to_backoff(session):
    value = session.get_sleep_time(response(""), 1)
    assert 0 < value <= session.max_backoff


def test_exponential_backoff_without_retry_after(session):
    assert session.get_sleep_time(response(), 0) == 0.0
    base = session.backoff_factor * 4
    assert base * 0.9 <= session.get_sleep_time(response(), 3) <= base * 1.1
    assert session.get_sleep_time(response(), 30) == session.max_backoff


def test_no_response_still_backs_off(session):
    assert session.get_sleep_time(None, 30) == session.max_backoff
