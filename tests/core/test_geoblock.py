"""Region checks for GEOFENCE and GEOBLOCK on the Service base class."""

from unshackle.core.service import Service


class Blocked(Service):
    GEOBLOCK = ("GB",)


class Fenced(Service):
    GEOFENCE = ("us",)


class Both(Service):
    GEOFENCE = ("us", "gb")
    GEOBLOCK = ("GB",)


def test_no_restrictions_allows_everything():
    assert Service.is_region_allowed("xx")
    assert Service.is_region_allowed(None)


def test_geoblock_refuses_listed_region_any_case():
    assert Blocked.is_region_allowed("de")
    assert not Blocked.is_region_allowed("gb")
    assert not Blocked.is_region_allowed("GB")


def test_geofence_allows_only_listed_regions():
    assert Fenced.is_region_allowed("US")
    assert not Fenced.is_region_allowed("gb")


def test_geoblock_wins_over_geofence():
    assert Both.is_region_allowed("us")
    assert not Both.is_region_allowed("gb")


def test_unknown_region_is_allowed():
    assert Blocked.is_region_allowed("")
    assert Blocked.is_region_allowed(None)
