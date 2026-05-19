"""Unit tests for metric calculations.

Uses synthetic bar fixtures (see conftest.make_bars) to assert exact metric
values for known inputs.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from ibkr_vol_screener.config import MarketBucket
from ibkr_vol_screener.metrics import compute_metrics, slice_last_60min
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


def test_slice_last_60min_keeps_only_recent(utc_now):
    bars = make_bars(utc_now - timedelta(minutes=120), count=120)
    sliced = slice_last_60min(bars, now=utc_now)
    assert 59 <= len(sliced) <= 61  # boundary tolerance
    assert sliced[-1].date == utc_now - timedelta(minutes=1)


def test_compute_metrics_flat_series_has_zero_range(utc_now):
    bars = make_bars(
        utc_now - timedelta(minutes=60),
        count=60,
        open_=100.0,
        step_pct=0.0,
        high_offset=0.0,
        low_offset=0.0,
        volume=1000,
    )
    row = compute_metrics(_cand(), bars, min_bars=10, now=utc_now)
    assert row is not None
    assert row.n_bars == 60
    assert row.range_pct == 0.0
    assert row.signed_return_pct == 0.0
    assert row.abs_return_pct == 0.0
    assert row.realized_vol_60m == 0.0
    assert row.volume_60m == 60_000


def test_compute_metrics_known_range_and_return(utc_now):
    # 60 bars stepping +0.1% each minute -> ~1.06x net.
    bars = make_bars(
        utc_now - timedelta(minutes=60),
        count=60,
        open_=100.0,
        step_pct=0.1,
        high_offset=0.0,
        low_offset=0.0,
        volume=2000,
    )
    row = compute_metrics(_cand(), bars, min_bars=10, now=utc_now)
    assert row is not None
    # Signed return: (last_close / first_open - 1) * 100.
    expected_signed = ((1.001**60) - 1) * 100.0
    assert math.isclose(row.signed_return_pct, expected_signed, rel_tol=1e-9)
    assert row.signed_return_pct > 0
    assert row.abs_return_pct == row.signed_return_pct
    # Range: high (last bar close) - low (first bar open) over last_close.
    expected_range = (bars[-1].close - bars[0].open) / bars[-1].close * 100.0
    assert math.isclose(row.range_pct, expected_range, rel_tol=1e-9)
    # 60 bars * 2000 volume each = 120k.
    assert row.volume_60m == 120_000
    # Each log return = ln(1.001) ≈ 0.0009995.
    assert row.realized_vol_60m > 0


def test_compute_metrics_returns_none_with_too_few_bars(utc_now):
    bars = make_bars(utc_now - timedelta(minutes=5), count=5)
    assert compute_metrics(_cand(), bars, min_bars=10, now=utc_now) is None


def test_compute_metrics_handles_zero_volume(utc_now):
    bars = make_bars(
        utc_now - timedelta(minutes=30),
        count=30,
        volume=0,
    )
    row = compute_metrics(_cand(), bars, min_bars=10, now=utc_now)
    assert row is not None
    assert row.volume_60m == 0
    assert row.dollar_volume_60m == 0


def test_compute_metrics_handles_negative_move(utc_now):
    bars = make_bars(
        utc_now - timedelta(minutes=60),
        count=60,
        open_=50.0,
        step_pct=-0.05,
        volume=500,
    )
    row = compute_metrics(_cand(), bars, min_bars=10, now=utc_now)
    assert row is not None
    assert row.signed_return_pct < 0
    assert row.abs_return_pct > 0
    assert row.abs_return_pct == abs(row.signed_return_pct)


def test_vwap_dev_pct_flat_series_is_zero(utc_now):
    bars = make_bars(
        utc_now - timedelta(minutes=60),
        count=60,
        open_=100.0,
        step_pct=0.0,
        high_offset=0.0,
        low_offset=0.0,
        volume=1000,
    )
    row = compute_metrics(_cand(), bars, min_bars=10, now=utc_now)
    assert row is not None
    assert math.isclose(row.vwap_60m, 100.0, rel_tol=1e-9)
    assert math.isclose(row.vwap_dev_pct, 0.0, abs_tol=1e-9)


def test_vwap_handles_zero_volume():
    """Volume sum of 0 -> VWAP defaults to 0 and vwap_dev_pct to 0 (not NaN)."""
    bars = make_bars(
        datetime(2026, 5, 19, 16, 0, tzinfo=UTC) - timedelta(minutes=60),
        count=60,
        volume=0,
    )
    row = compute_metrics(
        _cand(), bars, min_bars=10, now=datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    )
    assert row is not None
    assert row.vwap_60m == 0.0
    assert row.vwap_dev_pct == 0.0


def test_atr_pct_non_negative(utc_now):
    bars = make_bars(
        utc_now - timedelta(minutes=60),
        count=60,
        open_=100.0,
        step_pct=0.05,
        high_offset=0.5,
        low_offset=0.5,
        volume=1000,
    )
    row = compute_metrics(_cand(), bars, min_bars=10, now=utc_now)
    assert row is not None
    assert row.atr_pct_60m >= 0
    # Each bar has range = (high_offset + low_offset) = 1.0; ATR is ~ 1.0 / 100.
    assert 0.5 < row.atr_pct_60m < 1.5


def test_atr_captures_gap_between_bars(utc_now):
    """If close-to-next-low is larger than the bar's own range, TR should be that gap."""
    # Build bars manually: bar 0 close=100, bar 1 high=120 low=119 close=119.5
    # -> TR_1 should be max(120-119, |120-100|, |119-100|) = 20.
    times = [utc_now - timedelta(minutes=60 - i) for i in range(30)]
    bars = []
    for i, t in enumerate(times):
        if i == 0:
            bars.append(FakeBar(date=t, open=100, high=100.5, low=99.5, close=100, volume=1))
        elif i == 1:
            bars.append(FakeBar(date=t, open=119, high=120, low=119, close=119.5, volume=1))
        else:
            bars.append(FakeBar(date=t, open=119.5, high=120, low=119, close=119.5, volume=1))
    row = compute_metrics(_cand(), bars, min_bars=10, now=utc_now)
    assert row is not None
    # ATR has to be positive and reflect the big gap.
    assert row.atr_pct_60m > 0
    # mean TR includes one 20-point gap; with 30 bars, mean >= 20/30 = 0.66
    assert row.atr_pct_60m > 0.4


def test_gap_pct_none_when_no_prior_close(utc_now):
    bars = make_bars(utc_now - timedelta(minutes=60), count=60)
    row = compute_metrics(_cand(), bars, min_bars=10, now=utc_now)
    assert row is not None
    assert row.gap_pct is None


def test_gap_pct_computed_with_prior_close(utc_now):
    bars = make_bars(
        utc_now - timedelta(minutes=60), count=60, open_=110.0, step_pct=0.0
    )
    row = compute_metrics(
        _cand(), bars, min_bars=10, now=utc_now, prior_close=100.0
    )
    assert row is not None
    assert row.gap_pct is not None
    assert math.isclose(row.gap_pct, 10.0, rel_tol=1e-9)


def test_compute_metrics_normalizes_epoch_int_dates(utc_now):
    # Same bars but date stored as epoch int.
    bars = make_bars(utc_now - timedelta(minutes=60), count=60)
    epoch_bars = [
        FakeBar(
            date=int(b.date.replace(tzinfo=UTC).timestamp()),
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
        )
        for b in bars
    ]
    row = compute_metrics(_cand(), epoch_bars, min_bars=10, now=utc_now)
    assert row is not None
    assert row.n_bars >= 50
