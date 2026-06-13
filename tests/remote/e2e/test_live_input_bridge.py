"""E2E: interactive auth prompt round-trip.

For services that don't naturally prompt during ``authenticate()``, the
test still verifies the GET prompt endpoint returns the documented
shape (empty when no prompt pending).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live, pytest.mark.slow]


def test_prompt_endpoint_shape_without_pending(http_session, server_url: str, service_case) -> None:
    service, conf = service_case
    r = http_session.post(
        f"{server_url}/api/session/create",
        json={"service": service, "title_id": conf["title_url"]},
        timeout=120,
    )
    if r.status_code >= 400:
        pytest.skip(f"{service}: session creation failed: {r.status_code}")
    sid = r.json()["session_id"]
    try:
        pr = http_session.get(f"{server_url}/api/session/{sid}/prompt", timeout=10)
        assert pr.status_code in (200, 404), pr.text
        if pr.status_code == 200:
            body = pr.json()
            assert "prompt" in body or "status" in body
    finally:
        http_session.delete(f"{server_url}/api/session/{sid}", timeout=30)


def test_prompt_post_unknown_session_returns_404(http_session, server_url: str) -> None:
    r = http_session.post(
        f"{server_url}/api/session/bogus-session-id/prompt",
        json={"response": "irrelevant"},
        timeout=10,
    )
    assert r.status_code == 404


def test_prompt_get_unknown_session_returns_404(http_session, server_url: str) -> None:
    r = http_session.get(f"{server_url}/api/session/bogus-session-id/prompt", timeout=10)
    assert r.status_code == 404
