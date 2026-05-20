"""Tests for streaming.py: BarBuffer aggregation, pruning, backfill,
mocked StreamingSession lifecycle. No IBKR connection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from ibkr_vol_screener.config import MarketBucket
from ibkr_vol_screener.scanners import Candidate
from ibkr_vol_screener.streaming import (
    BarBuffer,
    StreamingSession,
    _aggregate_5s_to_1m,
    _StreamingBar,
)


def _bar(t: datetime, *, o=10.0, h=11.0, lo=9.0, c=10.5, v=100.0) -> _StreamingBar:
    return _StreamingBar(time=t, open=o, high=h, low=lo, close=c, volume=v)


# -------- aggregator --------


def test_aggregate_12_5s_bars_become_one_1min_bar():
    t = datetime(2026, 5, 19, 16, 0, 0, tzinfo=UTC)
    bars = [
        _bar(t + timedelta(seconds=5 * i), o=10 + i * 0.1, h=11 + i * 0.1,
             lo=9 + i * 0.1, c=10.5 + i * 0.1, v=100 + i)
        for i in range(12)
    ]
    aggregated = _aggregate_5s_to_1m(bars)
    assert len(aggregated) == 1
    bucket = aggregated[0]
    assert bucket.date == datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    assert bucket.open == bars[0].open
    assert bucket.close == bars[-1].close
    assert bucket.high == max(b.high for b in bars)
    assert bucket.low == min(b.low for b in bars)
    assert bucket.volume == sum(b.volume for b in bars)


def test_aggregate_two_minutes_produces_two_buckets():
    t0 = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=1)
    bars = [
        _bar(t0 + timedelta(seconds=5 * i)) for i in range(6)
    ] + [
        _bar(t1 + timedelta(seconds=5 * i)) for i in range(6)
    ]
    out = _aggregate_5s_to_1m(bars)
    assert len(out) == 2
    assert out[0].date == t0
    assert out[1].date == t1


def test_aggregate_partial_minute_handled():
    """7 5s bars in one minute -> one bucket with 7 bars' worth of OHLCV."""
    t = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    bars = [_bar(t + timedelta(seconds=5 * i), v=50.0) for i in range(7)]
    out = _aggregate_5s_to_1m(bars)
    assert len(out) == 1
    assert out[0].volume == 350.0


def test_aggregate_empty_input():
    assert _aggregate_5s_to_1m([]) == []


# -------- BarBuffer pruning + backfill --------


def test_buffer_append_and_aggregate():
    buf = BarBuffer(con_id=1, max_minutes=60)
    t = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    for i in range(12):
        buf.append(_bar(t + timedelta(seconds=5 * i), c=10.0 + i))
    bars = buf.to_1min_bars()
    assert len(bars) == 1
    assert bars[0].close == 21.0  # last close


def test_buffer_drops_old_bars():
    """Bars older than max_minutes are pruned on append."""
    buf = BarBuffer(con_id=1, max_minutes=2)
    base = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    # Old bar from 5 minutes ago.
    buf.append(_bar(base - timedelta(minutes=5)))
    # Fresh bar at base; pruning should drop the old one.
    buf.append(_bar(base))
    assert buf.n_live_5s_bars == 1


def test_buffer_backfill_seed_then_live_merge():
    """Backfilled minute should be replaced by live aggregate at the
    same minute."""

    @dataclass
    class _OneMin:
        date: datetime
        open: float
        high: float
        low: float
        close: float
        volume: float

    buf = BarBuffer(con_id=1, max_minutes=120)
    t = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    backfill = [_OneMin(t, 100, 101, 99, 100.5, 1000)]
    buf.append_backfill(backfill)
    # Live 5s bars at the SAME minute -> should override the backfill bar.
    for i in range(12):
        buf.append(_bar(t + timedelta(seconds=5 * i), c=200.0))
    merged = buf.to_1min_bars()
    # Exactly one bucket (the shared minute), close from live (200), not backfill (100.5).
    assert len(merged) == 1
    assert merged[0].close == 200.0


def test_buffer_backfill_disjoint_from_live():
    """Backfill at minute T and live bars at minute T+1 should both appear."""

    @dataclass
    class _OneMin:
        date: datetime
        open: float
        high: float
        low: float
        close: float
        volume: float

    buf = BarBuffer(con_id=1, max_minutes=120)
    t0 = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=1)
    buf.append_backfill([_OneMin(t0, 100, 101, 99, 100.5, 1000)])
    for i in range(12):
        buf.append(_bar(t1 + timedelta(seconds=5 * i), c=200.0))
    merged = buf.to_1min_bars()
    assert len(merged) == 2
    assert merged[0].date == t0
    assert merged[1].date == t1
    assert merged[0].close == 100.5
    assert merged[1].close == 200.0


def test_buffer_empty_returns_empty():
    buf = BarBuffer(con_id=1)
    assert buf.to_1min_bars() == []
    assert len(buf) == 0


# -------- StreamingSession with a fake IB --------


@dataclass
class _Contract:
    conId: int
    symbol: str
    exchange: str = "SMART"
    currency: str = "USD"


@dataclass
class _BarData:
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class _Event:
    """Tiny event surrogate matching the += / -= protocol ib_async uses."""

    def __init__(self):
        self.callbacks: list = []

    def __iadd__(self, cb):
        self.callbacks.append(cb)
        return self

    def __isub__(self, cb):
        if cb in self.callbacks:
            self.callbacks.remove(cb)
        return self


class _RealTimeBarList(list):
    def __init__(self):
        super().__init__()
        self.updateEvent = _Event()


class _FakeIB:
    def __init__(self):
        self.hist_calls: list[int] = []
        self.cancel_calls: list[_RealTimeBarList] = []
        self._real_time_lists: list[_RealTimeBarList] = []

    async def reqHistoricalDataAsync(self, contract, **kwargs):
        self.hist_calls.append(contract.conId)
        # Hand back 3 fake 1-min bars covering "the last hour".
        now = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
        return [
            _BarData(
                date=now - timedelta(minutes=3 - i),
                open=10.0, high=11.0, low=9.0, close=10.5, volume=1000,
            )
            for i in range(3)
        ]

    def reqRealTimeBars(self, contract, **kwargs):
        bl = _RealTimeBarList()
        self._real_time_lists.append(bl)
        return bl

    def cancelRealTimeBars(self, bar_list):
        self.cancel_calls.append(bar_list)


def _cand(con_id: int, sym: str = "X") -> Candidate:
    return Candidate(
        con_id=con_id, symbol=sym, exchange="SMART", primary_exchange="",
        currency="USD", bucket=MarketBucket.US_MAJOR,
        source_scan_codes={"HOT_BY_VOLUME"},
    )


@pytest.mark.asyncio
async def test_session_add_candidate_backfills_and_subscribes():
    ib = _FakeIB()
    session = StreamingSession(ib, max_subscriptions=5)
    await session.add_candidate(_cand(101, "AAA"))
    assert 101 in session.active_con_ids()
    assert ib.hist_calls == [101]
    # Buffer has the 3 backfill bars.
    bars = session.get_bars(101)
    assert len(bars) == 3
    await session.close()


@pytest.mark.asyncio
async def test_session_max_subscriptions_respected(caplog):
    ib = _FakeIB()
    session = StreamingSession(ib, max_subscriptions=2)
    await session.add_candidate(_cand(1))
    await session.add_candidate(_cand(2))
    await session.add_candidate(_cand(3))  # over the cap
    assert session.active_con_ids() == {1, 2}
    await session.close()


@pytest.mark.asyncio
async def test_session_remove_cancels_subscription():
    ib = _FakeIB()
    session = StreamingSession(ib)
    await session.add_candidate(_cand(7))
    assert 7 in session.active_con_ids()
    await session.remove_candidate(7)
    assert 7 not in session.active_con_ids()
    assert len(ib.cancel_calls) == 1


@pytest.mark.asyncio
async def test_session_sync_adds_and_removes():
    ib = _FakeIB()
    session = StreamingSession(ib)
    # Start with two.
    await session.sync_candidate_pool([_cand(1, "A"), _cand(2, "B")])
    assert session.active_con_ids() == {1, 2}
    # Next cycle: drop B, add C.
    added, removed = await session.sync_candidate_pool([_cand(1, "A"), _cand(3, "C")])
    assert added == 1
    assert removed == 1
    assert session.active_con_ids() == {1, 3}
    await session.close()


@pytest.mark.asyncio
async def test_session_live_bar_via_update_event_lands_in_buffer():
    ib = _FakeIB()
    session = StreamingSession(ib)
    await session.add_candidate(_cand(42, "Q"))
    # Simulate a live update: ib_async passes (RealTimeBarList, hasNewBar).
    bl = ib._real_time_lists[0]
    t = datetime(2026, 5, 19, 17, 0, tzinfo=UTC)

    @dataclass
    class _LiveBar:
        time: datetime
        open_: float
        high: float
        low: float
        close: float
        volume: float

    bl.append(_LiveBar(time=t, open_=20.0, high=21.0, low=19.0, close=20.5, volume=500))
    for cb in bl.updateEvent.callbacks:
        cb(bl, True)
    bars = session.get_bars(42)
    # Should now include the new minute (t) on top of the 3 backfill minutes.
    minutes = {b.date.minute for b in bars}
    assert 0 in minutes  # the 17:00 live minute is present
    await session.close()


@pytest.mark.asyncio
async def test_session_close_cancels_all():
    ib = _FakeIB()
    session = StreamingSession(ib)
    await session.add_candidate(_cand(1))
    await session.add_candidate(_cand(2))
    await session.close()
    assert len(ib.cancel_calls) == 2
    assert session.active_con_ids() == set()
