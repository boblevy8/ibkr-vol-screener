"""Tests for the --filter expression parser."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ibkr_vol_screener.filter_expr import (
    FilterParseError,
    compile_filters,
    parse_filter,
)
from ibkr_vol_screener.metrics import ScreenRow


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
        range_pct=5.0,
        signed_return_pct=10.0,
        abs_return_pct=10.0,
        volume_60m=500_000,
        dollar_volume_60m=5_500_000,
        realized_vol_60m=50.0,
        source_scan_codes=("HOT_BY_VOLUME",),
        atr_pct_60m=3.0,
        vwap_dev_pct=1.0,
        gap_pct=None,
        closes_60m=(10.0,) * 60,
    )
    base.update(overrides)
    return ScreenRow(**base)


def test_simple_gt():
    pred = parse_filter("range_pct>3")
    assert pred(_row(range_pct=5.0))
    assert not pred(_row(range_pct=1.0))


def test_simple_lt_eq():
    pred = parse_filter("volume_60m <= 1000000")
    assert pred(_row(volume_60m=500_000))
    assert pred(_row(volume_60m=1_000_000))
    assert not pred(_row(volume_60m=2_000_000))


def test_scientific_notation():
    pred = parse_filter("volume_60m > 1e6")
    assert pred(_row(volume_60m=2_000_000))
    assert not pred(_row(volume_60m=500_000))


def test_and_combinator():
    pred = parse_filter("range_pct > 3 AND volume_60m > 100000")
    assert pred(_row(range_pct=5, volume_60m=500_000))
    assert not pred(_row(range_pct=2, volume_60m=500_000))
    assert not pred(_row(range_pct=5, volume_60m=50_000))


def test_or_combinator():
    pred = parse_filter("range_pct > 10 OR atr_pct_60m > 2")
    assert pred(_row(range_pct=5, atr_pct_60m=3))   # matches via atr
    assert pred(_row(range_pct=15, atr_pct_60m=1))  # matches via range
    assert not pred(_row(range_pct=5, atr_pct_60m=1))


def test_parens_precedence():
    # Without parens AND binds tighter than OR:
    # A AND B OR C  ==  (A AND B) OR C
    # With parens we can group differently.
    pred = parse_filter("(range_pct > 10 OR atr_pct_60m > 5) AND volume_60m > 100000")
    assert pred(_row(range_pct=15, atr_pct_60m=1, volume_60m=200_000))
    assert pred(_row(range_pct=1, atr_pct_60m=10, volume_60m=200_000))
    # Fails the volume requirement:
    assert not pred(_row(range_pct=15, atr_pct_60m=10, volume_60m=50_000))
    # Fails the OR-group:
    assert not pred(_row(range_pct=5, atr_pct_60m=1, volume_60m=200_000))


def test_lowercase_and_or_accepted():
    pred = parse_filter("range_pct > 3 and atr_pct_60m > 2")
    assert pred(_row(range_pct=5, atr_pct_60m=3))


def test_none_attribute_evaluates_false():
    """gap_pct is None when --with-gap wasn't used; should not crash and
    should be filtered out by the gap predicate."""
    pred = parse_filter("gap_pct > 3")
    assert not pred(_row(gap_pct=None))
    assert pred(_row(gap_pct=5.0))


def test_unknown_metric_raises():
    with pytest.raises(FilterParseError, match="Unknown metric"):
        parse_filter("bogus_metric > 1")


def test_mismatched_parens_raises():
    with pytest.raises(FilterParseError, match="Missing"):
        parse_filter("(range_pct > 3 AND volume_60m > 100")


def test_missing_value_raises():
    with pytest.raises(FilterParseError):
        parse_filter("range_pct >")


def test_empty_expression_is_always_true():
    pred = parse_filter("")
    assert pred(_row())
    pred2 = parse_filter("   ")
    assert pred2(_row())


def test_compile_filters_ands_multiple():
    pred = compile_filters(["range_pct > 3", "atr_pct_60m > 2"])
    assert pred(_row(range_pct=5, atr_pct_60m=3))
    assert not pred(_row(range_pct=5, atr_pct_60m=1))
    assert not pred(_row(range_pct=1, atr_pct_60m=3))


def test_compile_filters_empty_list_is_always_true():
    pred = compile_filters([])
    assert pred(_row())


def test_negative_values():
    pred = parse_filter("signed_return_pct < -2")
    assert pred(_row(signed_return_pct=-5.0))
    assert not pred(_row(signed_return_pct=1.0))


def test_accel_factor_filter():
    pred = parse_filter("accel_factor > 1.5 AND range_pct > 2")
    assert pred(_row(accel_factor=2.0, range_pct=5.0))
    assert not pred(_row(accel_factor=0.5, range_pct=5.0))
    assert not pred(_row(accel_factor=2.0, range_pct=1.0))


def test_composite_score_filter():
    pred = parse_filter("composite_score > 5")
    assert pred(_row(composite_score=10.0))
    assert not pred(_row(composite_score=2.0))


def test_chained_and_three_terms():
    pred = parse_filter(
        "range_pct > 3 AND atr_pct_60m > 2 AND volume_60m > 100000"
    )
    assert pred(_row(range_pct=5, atr_pct_60m=3, volume_60m=200_000))
    assert not pred(_row(range_pct=5, atr_pct_60m=1, volume_60m=200_000))
