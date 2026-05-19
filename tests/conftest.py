"""Shared pytest fixtures and test helpers.

Provides:
  * `FakeBar` — minimal stand-in for `ib_async.BarData` with the attributes
    `metrics.compute_metrics` reads.
  * Builders for synthetic 1-minute bar series.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@dataclass
class FakeBar:
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def make_bars(
    start: datetime,
    *,
    count: int,
    open_: float = 100.0,
    step_pct: float = 0.0,
    high_offset: float = 0.5,
    low_offset: float = 0.5,
    volume: float = 1000,
) -> list[FakeBar]:
    """Generate `count` minute bars starting at `start`.

    Each close = prev_close * (1 + step_pct / 100). High/low bracket the close
    by the given offsets. Useful for asserting deterministic metric outputs.
    """
    bars: list[FakeBar] = []
    price = open_
    for i in range(count):
        ts = start + timedelta(minutes=i)
        bar_open = price
        bar_close = price * (1 + step_pct / 100.0)
        bar_high = max(bar_open, bar_close) + high_offset
        bar_low = min(bar_open, bar_close) - low_offset
        bars.append(
            FakeBar(
                date=ts,
                open=bar_open,
                high=bar_high,
                low=bar_low,
                close=bar_close,
                volume=volume,
            )
        )
        price = bar_close
    return bars


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def utc_now() -> datetime:
    # Pin "now" for deterministic 60-min slicing.
    return datetime(2026, 5, 19, 16, 0, 0, tzinfo=UTC)
