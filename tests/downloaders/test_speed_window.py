import pytest

from unshackle.core.downloaders.requests import SPEED_ROLLING_WINDOW, SpeedWindow


def test_rate_reflects_recent_window_not_full_history():
    """After a slow start (shared bucket), the display must show the current rate."""
    win = SpeedWindow(0.0)
    total = 0
    for t in range(0, 60):
        total += 5_000_000
        win.rate(float(t), total)
    for t in range(60, 120):
        total += 10_000_000
        rate = win.rate(float(t), total)
    assert rate == pytest.approx(10_000_000, rel=0.01)


def test_first_report_is_the_since_start_average():
    """Bytes are credited in lumps; the first lump must not read as a whole window's worth."""
    win = SpeedWindow(0.0)
    assert win.rate(2.5, 25_000_000) == pytest.approx(10_000_000)


def test_zero_span_returns_none():
    win = SpeedWindow(0.0)
    assert win.rate(0.0, 1_000_000) is None


def test_stall_returns_none():
    """Once the seed ages out, a total that stops growing has no delta to report."""
    win = SpeedWindow(0.0)
    for t in range(0, SPEED_ROLLING_WINDOW + 6):
        rate = win.rate(float(t), 1_000_000)
    assert rate is None


def test_window_drops_old_samples():
    win = SpeedWindow(0.0)
    for t in range(0, 3 * SPEED_ROLLING_WINDOW):
        win.rate(float(t), t * 1_000_000)
    assert win.samples[0][0] >= 2 * SPEED_ROLLING_WINDOW - 1
    assert len(win.samples) == SPEED_ROLLING_WINDOW + 1
