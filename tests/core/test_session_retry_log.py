"""RnetSession.request logs "Retrying" only when a retry follows."""

import logging
from types import SimpleNamespace

import pytest

from unshackle.core import session as session_module
from unshackle.core.session import MaxRetriesError, RnetSession


@pytest.fixture
def rate_limited(monkeypatch):
    s = RnetSession()
    monkeypatch.setattr(s, "ensure_client", lambda: SimpleNamespace(request=lambda *a, **k: None))
    monkeypatch.setattr(s, "get_sleep_time", lambda *a: 0)
    monkeypatch.setattr(
        session_module, "RnetResponse", lambda raw: SimpleNamespace(status_code=429, reason="Too Many Requests")
    )
    return s


@pytest.mark.parametrize("max_retries", [0, 2])
def test_retry_log_counts_only_real_retries(rate_limited, caplog, max_retries):
    with caplog.at_level(logging.WARNING), pytest.raises(MaxRetriesError):
        rate_limited.get("https://example.invalid/x", max_retries=max_retries)

    retries = [r.getMessage() for r in caplog.records if "Retrying" in r.getMessage()]
    assert retries == [f"429 Too Many Requests(/x). Retrying... ({n}/{max_retries})" for n in range(1, max_retries + 1)]


@pytest.mark.parametrize(("max_retries", "attempts"), [(0, ""), (2, " after 3 attempts")])
def test_spent_budget_names_the_attempts_and_host_not_the_url(rate_limited, max_retries, attempts):
    with pytest.raises(MaxRetriesError) as raised:
        rate_limited.get("https://example.invalid/x?hmac=secret", max_retries=max_retries)

    assert str(raised.value) == f"GET example.invalid failed{attempts}: 429 Too Many Requests"
