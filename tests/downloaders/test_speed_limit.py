import importlib
import inspect
import threading
import time

import pytest

from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.downloaders.requests import TokenBucket, format_speed, parse_speed_limit, set_speed_limit

requests_downloader = importlib.import_module("unshackle.core.downloaders.requests")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("500k", 500_000),
        ("5M", 5_000_000),
        ("10MB", 10_000_000),
        ("10mb/s", 10_000_000),
        ("1.5G", 1_500_000_000),
        ("1048576", 1_048_576),
        (5_000_000, 5_000_000.0),
        (None, None),
        ("off", None),
        ("none", None),
        ("unlimited", None),
        ("0", None),
        (0, None),
    ],
)
def test_parse_speed_limit(value, expected):
    assert parse_speed_limit(value) == expected


@pytest.mark.parametrize(
    "value",
    ["fast", "10TB", "10MiB", "-5M", "M", "1..5M", ".", "1.2.3", float("inf"), float("nan"), -5, -1.5],
)
def test_parse_speed_limit_rejects(value):
    with pytest.raises(ValueError):
        parse_speed_limit(value)


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        (500, "500 bytes/s"),
        (512_000, "512.0 kB/s"),
        (39 * 1024**2, "40.9 MB/s"),
        (1.5 * 1024**3, "1.6 GB/s"),
    ],
)
def test_format_speed(rate, expected):
    assert format_speed(rate) == expected


def test_result_accepts_a_missing_speed_limit():
    """The API/serve path calls dl.result() by keyword without speed_limit."""
    from unshackle.commands.dl import dl

    assert inspect.signature(dl.result).parameters["speed_limit"].default is None


def test_bucket_holds_aggregate_rate_across_threads():
    rate = 10_000_000
    bucket = TokenBucket(rate)
    chunk = 100_000
    totals = [0, 0, 0, 0]
    start = time.monotonic()
    stop = start + 1.0

    def worker(i: int) -> None:
        while time.monotonic() < stop:
            bucket.consume(chunk)
            totals[i] += chunk

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    achieved = sum(totals) / (time.monotonic() - start)
    # exceeding the limit is a real failure; undershoot is scheduler noise on a loaded box
    assert achieved <= rate * 1.1
    assert achieved >= rate * 0.5


def test_bucket_chunk_larger_than_capacity_does_not_deadlock():
    bucket = TokenBucket(1_000_000)
    start = time.monotonic()
    bucket.consume(1_100_000)
    assert 1.0 < time.monotonic() - start < 2.0


def test_locked_limit_ignores_unlocked_calls():
    """serve's global_speed_limit must survive the per-job set in dl.result()."""
    try:
        set_speed_limit(5_000_000, lock=True)
        set_speed_limit(None)
        set_speed_limit(1_000_000)
        limiter = requests_downloader._speed_limiter
        assert limiter is not None and limiter.rate == 5_000_000
    finally:
        requests_downloader._speed_limit_locked = False
        requests_downloader._speed_limiter = None


def test_bucket_wait_aborts_on_cancellation():
    """Debt sleeps grow with thread count; cancelling must not wait them out."""
    bucket = TokenBucket(1_000)
    DOWNLOAD_CANCELLED.set()
    try:
        start = time.monotonic()
        bucket.consume(1_000_000)
        assert time.monotonic() - start < 1.0
    finally:
        DOWNLOAD_CANCELLED.clear()
