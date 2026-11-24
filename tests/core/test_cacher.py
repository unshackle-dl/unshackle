import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonpickle
import jwt
import pytest

from unshackle.core import cacher
from unshackle.core.cacher import Cacher

CLOSE = timedelta(seconds=5)


def set_zone(monkeypatch: pytest.MonkeyPatch, zone: str) -> None:
    monkeypatch.setenv("TZ", zone)
    time.tzset()


@pytest.fixture
def zone(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Let a test change the process time zone, and put it back afterwards."""
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset")
    yield lambda name: set_zone(monkeypatch, name)
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Cacher:
    monkeypatch.setattr(cacher.config.directories, "cache", tmp_path)
    return Cacher("TAG")


def test_seconds_until_expiration_ignore_the_1970_utc_offset(zone) -> None:  # type: ignore[no-untyped-def]
    zone("Asia/Kathmandu")
    if time.localtime(0).tm_gmtoff == time.localtime().tm_gmtoff:
        pytest.skip("zone data missing: the 1970 offset equals the current one")
    assert abs(Cacher.resolve_datetime(283) - (datetime.now() + timedelta(seconds=283))) < CLOSE


@pytest.mark.parametrize("value", [283, 283.5, "283"])
def test_small_number_is_seconds_until_expiration(value: object) -> None:
    assert abs(Cacher.resolve_datetime(value) - (datetime.now() + timedelta(seconds=283))) < CLOSE  # type: ignore[arg-type]


@pytest.mark.parametrize("offset", [-3600, 3600])
def test_unix_timestamp_is_absolute_also_when_it_is_in_the_past(offset: int) -> None:
    stamp = int(time.time()) + offset
    assert Cacher.resolve_datetime(stamp) == datetime.fromtimestamp(stamp)
    assert Cacher.resolve_datetime(stamp * 1000) == datetime.fromtimestamp(stamp)


def test_iso_string_with_utc_offset_becomes_naive_local_time() -> None:
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    expected = datetime.fromtimestamp(moment.timestamp())
    assert Cacher.resolve_datetime(moment.isoformat()) == expected
    assert Cacher.resolve_datetime(moment.strftime("%Y-%m-%dT%H:%M:%SZ")) == expected
    assert Cacher.resolve_datetime(moment) == expected


@pytest.mark.parametrize("value", [float("inf"), 10**17, "not a time"])
def test_unusable_value_raises_value_error(value: object) -> None:
    with pytest.raises(ValueError):
        Cacher.resolve_datetime(value)  # type: ignore[arg-type]


def test_zero_seconds_expires_now(cache: Cacher) -> None:
    entry = cache.get("key")
    entry.set({"a": 1}, expiration=0)
    assert entry.expired


def test_expired_jwt_is_stored_as_expired(cache: Cacher) -> None:
    entry = cache.get("key")
    entry.set(jwt.encode({"exp": int(time.time()) - 60}, "k" * 32, algorithm="HS256"))
    assert entry.expired


def test_expiration_survives_a_change_of_time_zone(cache: Cacher, zone) -> None:  # type: ignore[no-untyped-def]
    zone("UTC")
    cache.get("key").set({"a": 1}, expiration=300)
    zone("America/Denver")
    entry = cache.get("key")
    assert entry.data == {"a": 1}
    assert abs(entry.expiration - (datetime.now() + timedelta(seconds=300))) < CLOSE


def test_entry_without_expires_at_reads_its_local_expiration(cache: Cacher) -> None:
    entry = cache.get("key")
    entry.set({"a": 1}, expiration=300)
    payload = jsonpickle.loads(entry.path.read_text(encoding="utf8"))
    del payload["expires_at"], payload["crc32"]
    payload["crc32"] = cacher.zlib.crc32(jsonpickle.dumps(payload).encode("utf8"))
    entry.path.write_text(jsonpickle.dumps(payload), encoding="utf8")

    legacy = cache.get("key")
    assert not legacy.expired
    assert abs(legacy.expiration - (datetime.now() + timedelta(seconds=300))) < CLOSE
