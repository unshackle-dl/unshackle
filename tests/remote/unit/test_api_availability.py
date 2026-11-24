"""Availability stays optional, allowlisted and separate from private service data."""

import pytest

from unshackle.core.api.handlers import serialize_title
from unshackle.core.remote_service import build_title
from unshackle.core.titles.availability import availability_metadata
from unshackle.core.titles.episode import Episode

pytestmark = pytest.mark.unit


class EXAMPLE:
    pass


def episode(data=None):
    return Episode(id_="ep-0001", service=EXAMPLE, title="Show", season=1, number=1, data=data)


@pytest.mark.parametrize("value", [None, [], "denied", {}, {"paid": 1}, {"access": "false"}, {"access": []}])
def test_invalid_metadata_is_omitted(value):
    assert availability_metadata(value) is None
    assert "availability" not in serialize_title(episode({"availability": value}))


@pytest.mark.parametrize("access", ["granted", "denied", "unknown"])
def test_paid_and_access_are_independent_and_round_trip(access):
    data = {"paid": True, "access": access}
    serialized = serialize_title(episode({"availability": data}))
    assert serialized["availability"] == data
    assert build_title(serialized, "EXAMPLE", "fallback").data["availability"] == data


def test_private_data_and_unknown_fields_never_leak():
    data = {"availability": {"paid": False, "access": "unknown", "token": "secret"}, "cookies": "secret"}
    result = serialize_title(episode(data))
    assert result["availability"] == {"paid": False, "access": "unknown"}
    assert "secret" not in str(result)
    assert data["availability"]["token"] == "secret"


def test_missing_metadata_keeps_response_unchanged():
    assert "availability" not in serialize_title(episode())


def test_remote_input_is_also_allowlisted():
    info = serialize_title(episode())
    info["availability"] = {"paid": True, "access": "denied", "cookies": "secret"}
    rebuilt = build_title(info, "EXAMPLE", "fallback")
    assert rebuilt.data["availability"] == {"paid": True, "access": "denied"}


def test_availability_appends_without_reordering_existing_fields():
    title = episode()
    title.part = 2
    title.anime = True
    original = serialize_title(title)
    title.data = {"availability": {"paid": True, "access": "granted"}}
    serialized = serialize_title(title)
    assert list(serialized) == [*original, "availability"]
    assert {key: serialized[key] for key in original} == original
