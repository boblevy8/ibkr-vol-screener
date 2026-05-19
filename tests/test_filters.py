"""Unit tests for reporting.filter_rows and rank_rows."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ibkr_vol_screener.config import MarketBucket, default_profile
from ibkr_vol_screener.metrics import ScreenRow
from ibkr_vol_screener.reporting import filter_rows, rank_rows


def _row(
    symbol: str,
    bucket: MarketBucket,
    *,
    last_close: float,
    range_pct: float,
    abs_return_pct: float,
    volume: float,
    dollar_volume: float,
) -> ScreenRow:
    t = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    return ScreenRow(
        symbol=symbol,
        con_id=hash(symbol) & 0xFFFFFFF,
        bucket=bucket.value,
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        first_bar_time=t,
        last_bar_time=t,
        n_bars=60,
        first_open=last_close,
        last_close=last_close,
        high_60m=last_close * 1.05,
        low_60m=last_close * 0.95,
        range_pct=range_pct,
        signed_return_pct=abs_return_pct,
        abs_return_pct=abs_return_pct,
        volume_60m=volume,
        dollar_volume_60m=dollar_volume,
        realized_vol_60m=0.0,
        source_scan_codes=("HOT_BY_VOLUME",),
    )


def test_filter_drops_below_min_price():
    profiles = {b: default_profile(b) for b in MarketBucket}
    rows = [
        _row("CHEAP", MarketBucket.US_MAJOR, last_close=0.5, range_pct=10,
             abs_return_pct=5, volume=1_000_000, dollar_volume=10_000_000),
        _row("OK", MarketBucket.US_MAJOR, last_close=10, range_pct=10,
             abs_return_pct=5, volume=1_000_000, dollar_volume=10_000_000),
    ]
    kept = filter_rows(rows, profiles)
    assert {r.symbol for r in kept} == {"OK"}


def test_filter_otc_stricter_than_us():
    profiles = {b: default_profile(b) for b in MarketBucket}
    # Each row has volume 100k. OTC requires 200k min so it should drop.
    # US major requires 50k so it should keep.
    us = _row("US1", MarketBucket.US_MAJOR, last_close=5, range_pct=8,
              abs_return_pct=3, volume=100_000, dollar_volume=500_000)
    otc = _row("PINK", MarketBucket.OTC, last_close=0.5, range_pct=20,
               abs_return_pct=10, volume=100_000, dollar_volume=50_000)
    kept = filter_rows([us, otc], profiles)
    assert {r.symbol for r in kept} == {"US1"}


def test_filter_max_price_optional():
    from dataclasses import replace

    profiles = {b: default_profile(b) for b in MarketBucket}
    profiles[MarketBucket.US_MAJOR] = replace(
        profiles[MarketBucket.US_MAJOR], max_price=50.0
    )
    rows = [
        _row("LOW", MarketBucket.US_MAJOR, last_close=10, range_pct=10,
             abs_return_pct=5, volume=1_000_000, dollar_volume=10_000_000),
        _row("HIGH", MarketBucket.US_MAJOR, last_close=200, range_pct=10,
             abs_return_pct=5, volume=1_000_000, dollar_volume=10_000_000),
    ]
    kept = filter_rows(rows, profiles)
    assert {r.symbol for r in kept} == {"LOW"}


def test_rank_sorts_by_range_pct_desc():
    rows = [
        _row("A", MarketBucket.US_MAJOR, last_close=10, range_pct=3,
             abs_return_pct=1, volume=1_000_000, dollar_volume=10_000_000),
        _row("B", MarketBucket.US_MAJOR, last_close=10, range_pct=15,
             abs_return_pct=5, volume=2_000_000, dollar_volume=20_000_000),
        _row("C", MarketBucket.US_MAJOR, last_close=10, range_pct=8,
             abs_return_pct=2, volume=500_000, dollar_volume=5_000_000),
    ]
    out = rank_rows(rows, sort_key="range_pct", top_n=2)
    assert [r.symbol for r in out] == ["B", "C"]


def test_rank_invalid_sort_key_raises():
    with pytest.raises(ValueError):
        rank_rows([], sort_key="not_a_key")
