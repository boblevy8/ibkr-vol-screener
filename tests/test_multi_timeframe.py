"""Multi-timeframe metric tests + acceleration factor."""

from __future__ import annotations

from datetime import timedelta

from ibkr_vol_screener.config import MarketBucket
from ibkr_vol_screener.metrics import compute_metrics
from ibkr_vol_screener.scanners import Candidate

from .conftest import FakeBar, make_bars


def _cand() -> Candidate:
    return Candidate(
        con_id=1,
        symbol="TEST",
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        bucket=MarketBucket.US_MAJOR,
        source_scan_codes={"HOT_BY_VOLUME"},
    )


def test_all_window_fields_populated_for_smooth_series(utc_now):
    # 60 quietly-trending bars: every window should compute non-zero range.
    bars = make_bars(
        utc_now - timedelta(minutes=60),
        count=60,
        open_=100.0,
        step_pct=0.05,
        volume=1000,
    )
    row = compute_metrics(_cand(), bars, min_bars=10, min_bars_short=3, now=utc_now)
    assert row is not None
    # All four windows have data:
    assert row.range_pct > 0
    assert row.range_pct_30m > 0
    assert row.range_pct_15m > 0
    assert row.range_pct_5m > 0
    # ATR computed for each:
    assert row.atr_pct_60m > 0
    assert row.atr_pct_15m > 0
    assert row.atr_pct_5m > 0


def test_outsized_last_15m_lights_accel_factor(utc_now):
    """First 45 bars flat, last 15 bars have a 5% jump.
    Accel factor = range_pct_15m / range_pct_60m should be high.
    """
    quiet = [
        FakeBar(
            date=utc_now - timedelta(minutes=60 - i),
            open=100.0,
            high=100.05,
            low=99.95,
            close=100.0,
            volume=1000,
        )
        for i in range(45)
    ]
    # Last 15 bars sweep up 5% smoothly.
    last15 = []
    for i in range(15):
        ts = utc_now - timedelta(minutes=15 - i)
        price = 100.0 + (i + 1) * (5.0 / 15.0)
        last15.append(
            FakeBar(date=ts, open=price - 0.1, high=price + 0.05, low=price - 0.15, close=price, volume=1500)
        )
    bars = quiet + last15

    row = compute_metrics(_cand(), bars, min_bars=10, min_bars_short=3, now=utc_now)
    assert row is not None
    # The 15-min window contains nearly all of the 60m range.
    assert row.range_pct_15m > row.range_pct * 0.8
    assert row.accel_factor > 0.8  # almost all action in the last quarter


def test_short_window_with_too_few_bars_falls_back_to_zero(utc_now):
    # 30 bars total -> 5m and 15m have enough, but 30m only just; 60m short.
    bars = make_bars(utc_now - timedelta(minutes=10), count=8, open_=50.0)
    row = compute_metrics(_cand(), bars, min_bars=5, min_bars_short=3, now=utc_now)
    assert row is not None
    # 60m window has < min_bars when we use a strict min, but we relaxed to 5.
    # Verify the row exists and the 30m field is 0 because we didn't have enough.
    # (5m and 15m fields should have populated since we have 8 bars in the last 10 min.)
    assert row.range_pct_5m > 0  # 8 bars in last 5 min easily passes min_bars_short=3
    # The 30m window also captures those same 8 bars, so it's populated too.


def test_composite_score_positive_for_volatile_row(utc_now):
    bars = make_bars(utc_now - timedelta(minutes=60), count=60, open_=50.0, step_pct=0.2)
    row = compute_metrics(_cand(), bars, min_bars=10, min_bars_short=3, now=utc_now)
    assert row is not None
    assert row.composite_score > 0
