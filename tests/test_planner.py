"""Tests for the position-sizing trade planner (no IBKR)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ibkr_vol_screener.metrics import ScreenRow
from ibkr_vol_screener.planner import compute_plan


def _row(**overrides) -> ScreenRow:
    t = datetime(2026, 5, 20, 16, 0, tzinfo=UTC)
    base = dict(
        symbol="TEST",
        con_id=1,
        bucket="us_major",
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        first_bar_time=t,
        last_bar_time=t,
        n_bars=60,
        first_open=100.0,
        last_close=100.0,
        high_60m=102.0,
        low_60m=98.0,
        range_pct=4.0,
        signed_return_pct=0.0,
        abs_return_pct=0.0,
        volume_60m=1_000_000,
        dollar_volume_60m=100_000_000,
        realized_vol_60m=50.0,
        source_scan_codes=(),
        vwap_60m=100.0,
        vwap_dev_pct=0.0,
        atr_pct_60m=2.0,  # 2% ATR -> $2 on a $100 stock
    )
    base.update(overrides)
    return ScreenRow(**base)


def test_long_plan_math_matches_hand_calc():
    """entry=100, atr_pct=2 -> atr_dollar=2; atr_mul=1.5 -> stop_dist=3;
    target_r=2 -> target_dist=6.
    risk_pct=1% of 50000 = 500; size = 500/3 = 166 shares."""
    plan = compute_plan(
        _row(),
        account_value=50_000,
        risk_pct=1.0,
        side="long",
        atr_multiple=1.5,
        target_r=2.0,
    )
    assert plan.entry == 100.0
    assert plan.stop_distance == 3.0
    assert plan.target_distance == 6.0
    assert plan.stop == 97.0
    assert plan.target == 106.0
    assert plan.risk_dollar == 500.0
    assert plan.position_size == 166  # floor(500/3)
    assert plan.notional == pytest.approx(166 * 100.0)
    assert plan.r_multiple == 2.0


def test_short_plan_inverts_stop_and_target():
    plan = compute_plan(
        _row(),
        account_value=50_000,
        risk_pct=1.0,
        side="short",
        atr_multiple=1.5,
        target_r=2.0,
    )
    assert plan.stop == 103.0
    assert plan.target == 94.0
    # Position size same regardless of side.
    assert plan.position_size == 166


def test_position_size_floored_not_rounded():
    """500 / 7 = 71.42 -> 71 shares, not 72."""
    row = _row(last_close=100.0, atr_pct_60m=4.667)  # atr_dollar≈4.667; stop_dist≈7
    plan = compute_plan(row, account_value=50_000, risk_pct=1.0)
    # Verify floor behavior — never over-risk.
    assert plan.position_size == int(500 // plan.stop_distance)


def test_zero_atr_raises():
    with pytest.raises(ValueError, match="ATR"):
        compute_plan(_row(atr_pct_60m=0.0), account_value=50_000)


def test_invalid_side_raises():
    with pytest.raises(ValueError, match="side"):
        compute_plan(_row(), account_value=50_000, side="sideways")


def test_invalid_risk_pct_raises():
    with pytest.raises(ValueError, match="risk_pct"):
        compute_plan(_row(), account_value=50_000, risk_pct=0)
    with pytest.raises(ValueError, match="risk_pct"):
        compute_plan(_row(), account_value=50_000, risk_pct=150)


def test_invalid_account_value_raises():
    with pytest.raises(ValueError, match="account_value"):
        compute_plan(_row(), account_value=0)


def test_notional_pct_of_account_computed():
    plan = compute_plan(_row(), account_value=50_000, risk_pct=1.0)
    expected = plan.notional / 50_000 * 100
    assert plan.notional_pct_of_account == pytest.approx(expected)


def test_r_multiple_reflects_target_r():
    plan = compute_plan(_row(), account_value=50_000, target_r=3.0)
    assert plan.r_multiple == 3.0


def test_custom_atr_multiple_changes_stop_distance():
    plan = compute_plan(_row(), account_value=50_000, atr_multiple=2.5)
    # atr_dollar=2; stop_dist = 2.5 * 2 = 5
    assert plan.stop_distance == 5.0
    # 500 / 5 = 100 shares
    assert plan.position_size == 100
