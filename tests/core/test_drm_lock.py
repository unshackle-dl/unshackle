"""Unit tests for dl.drm_lock, the per-key-set lock that makes tracks sharing KIDs
take turns so the second one reads the content keys out of the cache."""

from __future__ import annotations

from threading import Lock
from typing import Any, Optional
from uuid import UUID

import pytest

from unshackle.commands.dl import dl

pytestmark = pytest.mark.unit

KID_A = UUID("11111111111111111111111111111111")
KID_B = UUID("22222222222222222222222222222222")


class FakeDRM:
    def __init__(self, kids: Optional[list[Any]] = None, content_id: Optional[str] = None) -> None:
        self.kids = kids or []
        self.content_id = content_id


class NamelessDRM:
    """No kids and no content_id, so the class name is the only key left."""

    pass


@pytest.fixture(autouse=True)
def clean_locks() -> Any:
    dl.DRM_LOCKS.clear()
    yield
    dl.DRM_LOCKS.clear()


def test_same_kid_set_shares_one_lock() -> None:
    lock = dl.drm_lock(FakeDRM(kids=[KID_A, KID_B]))
    assert isinstance(lock, type(Lock()))
    assert dl.drm_lock(FakeDRM(kids=[KID_A, KID_B])) is lock


def test_kid_order_does_not_matter() -> None:
    first = dl.drm_lock(FakeDRM(kids=[KID_A, KID_B]))
    assert dl.drm_lock(FakeDRM(kids=[KID_B, KID_A])) is first


def test_different_kids_get_different_locks() -> None:
    assert dl.drm_lock(FakeDRM(kids=[KID_A])) is not dl.drm_lock(FakeDRM(kids=[KID_B]))
    assert len(dl.DRM_LOCKS) == 2


def test_no_kids_falls_back_to_content_id() -> None:
    lock = dl.drm_lock(FakeDRM(content_id="abc"))
    assert dl.drm_lock(FakeDRM(content_id="abc")) is lock
    assert dl.drm_lock(FakeDRM(content_id="def")) is not lock
    assert set(dl.DRM_LOCKS) == {"abc", "def"}


def test_no_kids_and_no_content_id_falls_back_to_class_name() -> None:
    lock = dl.drm_lock(NamelessDRM())
    assert dl.drm_lock(NamelessDRM()) is lock
    assert set(dl.DRM_LOCKS) == {"NamelessDRM"}


def test_empty_content_id_still_falls_back_to_class_name() -> None:
    dl.drm_lock(FakeDRM(content_id=""))
    assert set(dl.DRM_LOCKS) == {"FakeDRM"}
