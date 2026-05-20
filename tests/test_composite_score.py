"""Composite-score weighting + clipping tests (no IBKR required)."""

from __future__ import annotations

from datetime import UTC, datetime

from ibkr_vol_screener.metrics import (
    DEFAULT_SCORE_WEIGHTS,
    ScreenRow,
    compute_composite_score,
)


def _row(**overrides) -> ScreenRow:
    t = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    base = dict(
        symbol="AAA",
        con_id=1,
        bucket="us_major",
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        first_bar_time=t,
        last_bar_time=t,
        n_bars=60,
        first_open=10.0,
        last_close=11.0,
        high_60m=12.0,
        low_60m=10.0,
        range_pct=0.0,
        signed_return_pct=0.0,
        abs_return_pct=0.0,
        volume_60m=0.0,
        dollar_volume_60m=0.0,
        realized_vol_60m=0.0,
        source_scan_codes=(),
        vwap_60m=10.5,
        vwap_dev_pct=0.0,
        atr_pct_60m=0.0,
        accel_factor=0.0,
    )
    base.update(overrides)
    return ScreenRow(**base)


def test_zero_row_scores_zero():
    row = _row()
    assert compute_composite_score(row, DEFAULT_SCORE_WEIGHTS) == 0.0


def test_known_weights_give_known_score():
    weights = {"range_pct": 1.0}  # only range_pct counts
    row = _row(range_pct=5.0)
    assert compute_composite_score(row, weights) == 5.0


def test_signed_magnitude_via_vwap_dev_abs():
    weights = {"vwap_dev_abs": 1.0}
    pos = _row(vwap_dev_pct=3.0)
    neg = _row(vwap_dev_pct=-3.0)
    assert compute_composite_score(pos, weights) == compute_composite_score(neg, weights)


def test_clipping_caps_extreme_values():
    weights = {"range_pct": 1.0}
    huge = _row(range_pct=10_000.0)  # absurd
    sane = _row(range_pct=50.0)
    # range_pct clip is 50.0 -> huge clips down to 50.
    assert compute_composite_score(huge, weights) == 50.0
    assert compute_composite_score(sane, weights) == 50.0


def test_unknown_weight_key_silently_ignored():
    weights = {"bogus_metric": 1.0, "range_pct": 0.5}
    row = _row(range_pct=10.0)
    assert compute_composite_score(row, weights) == 5.0


def test_default_weights_sum_to_one():
    # Sanity check: defaults are normalized so the score is roughly
    # comparable to typical per-metric magnitudes.
    assert abs(sum(DEFAULT_SCORE_WEIGHTS.values()) - 1.0) < 1e-9


def test_full_blend():
    weights = DEFAULT_SCORE_WEIGHTS
    row = _row(
        range_pct=4.0,
        atr_pct_60m=1.0,
        vwap_dev_pct=2.0,
        accel_factor=1.5,
        realized_vol_60m=100.0,
    )
    score = compute_composite_score(row, weights)
    # Hand calc: 0.3*4 + 0.2*1 + 0.15*2 + 0.25*1.5 + 0.10*100
    # = 1.2 + 0.2 + 0.3 + 0.375 + 10.0
    # = 12.075
    assert abs(score - 12.075) < 1e-9
