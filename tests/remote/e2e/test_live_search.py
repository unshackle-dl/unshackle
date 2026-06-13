"""E2E: search endpoint per service."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live, pytest.mark.slow]


def test_search_returns_results(http_session, server_url: str, service_case) -> None:
    service, conf = service_case
    query = conf.get("search_query")
    if not query:
        pytest.skip(f"no search_query configured for {service}")

    r = http_session.post(
        f"{server_url}/api/search",
        json={"service": service, "query": query},
        timeout=120,
    )
    if r.status_code in (400, 401, 403):
        pytest.skip(f"{service} search not available: {r.status_code} {r.text[:200]}")
    if r.status_code == 502 and "not supported" in r.text.lower():
        pytest.skip(f"{service} does not implement search")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "results" in body
    assert isinstance(body["results"], list)
