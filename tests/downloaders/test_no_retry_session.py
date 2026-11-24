"""Connection pooling survives the no-retry adapter view.

``download()`` builds this view once per segment. ``copy()`` runs
``HTTPAdapter.__setstate__``, which blanks ``proxy_manager`` and re-runs
``init_poolmanager()``, so a view that keeps those fresh objects hands every segment an
empty pool: one TCP connection and one TLS handshake each. That is invisible on Linux and
costs a native Windows host roughly 7x the wall time and 4x the CPU on a manifest of a few
thousand segments.

The second test pins the reason sharing one pool between the two adapters is safe:
``HTTPAdapter.send`` passes ``retries=self.max_retries`` per call, so the view's ``Retry(0)``
travels with the view and never reaches the caller's own session.
"""

from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from unshackle.core.downloaders.requests import no_retry_session


def pooled_session(total: int = 5) -> Session:
    session = Session()
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16, pool_block=True, max_retries=Retry(total=total))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def test_view_shares_the_pools_it_was_copied_from() -> None:
    session = pooled_session()
    view = no_retry_session(session)

    for prefix, adapter in session.adapters.items():
        twin = view.adapters[prefix]
        assert twin is not adapter, "the view must be its own adapter, or Retry(0) lands on the caller"
        assert twin.poolmanager is adapter.poolmanager
        assert twin.proxy_manager is adapter.proxy_manager


def test_view_never_retries_and_leaves_the_caller_retrying() -> None:
    session = pooled_session(total=5)
    view = no_retry_session(session)

    assert view.adapters["https://"].max_retries.total == 0
    assert session.adapters["https://"].max_retries.total == 5, "the caller's retry policy must survive"


def test_repeated_views_keep_reusing_one_pool() -> None:
    """``download()`` rebuilds the view per segment; every rebuild must land on the same pool."""
    session = pooled_session()
    origin = session.adapters["https://"].poolmanager

    for _ in range(3):
        assert no_retry_session(session).adapters["https://"].poolmanager is origin
