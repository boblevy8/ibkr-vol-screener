"""Live 5-second bar streaming + rolling buffer for watch mode.

The legacy watch flow re-fetched a full 60-minute window of 1-min bars
for every candidate every cycle. That wastes 98% of the data (it's the
same window shifted by 60 seconds) and hits IBKR pacing limits at
30-second refresh intervals.

This module replaces that with persistent `reqRealTimeBars`
subscriptions. Each candidate gets:

  1. A one-shot `reqHistoricalData` backfill to populate a rolling
     buffer with the last ~75 minutes of 1-min bars.
  2. A continuous `reqRealTimeBars(barSize=5, whatToShow='TRADES')`
     subscription that pushes a 5-second bar every 5 seconds.
  3. A rolling buffer that drops bars older than `max_minutes`.

`BarBuffer.to_1min_bars()` returns a chronological list of bars that
implements the existing `BarLike` Protocol — drop-in compatible with
`compute_metrics` and every downstream module.

This module is read-only: `reqRealTimeBars` / `cancelRealTimeBars`
are data subscription endpoints, not order endpoints. The AST scan
in `tests/test_readonly.py` does not forbid them.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ib_async import IB

    from .scanners import Candidate

log = logging.getLogger(__name__)


# ---- bar shapes ----


@dataclass(frozen=True)
class _StreamingBar:
    """A single 5-second bar from `reqRealTimeBars`."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class _AggregatedBar:
    """A 1-minute bar, either backfilled from `reqHistoricalData` or
    aggregated from 5-second streaming bars. Implements `BarLike`.
    """

    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _to_utc(value: Any) -> datetime:
    """Normalize any datetime-ish thing to a tz-aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    # Fall through: stringy / date-only inputs are rare for live bars.
    raise TypeError(f"Cannot normalize bar time={value!r}")


def _minute_bucket(t: datetime) -> datetime:
    """Floor a datetime to the minute (used to group 5s bars)."""
    return t.replace(second=0, microsecond=0)


# ---- buffer ----


class BarBuffer:
    """Rolling buffer of streaming + backfilled bars for one symbol.

    Two storage tiers:
      * `_backfill_bars`: 1-min bars seeded once from
        `reqHistoricalData`. These cover everything *before* the
        live subscription started.
      * `_live_bars`: deque of 5s bars from `reqRealTimeBars`.
        Aggregated to 1-min on demand.

    Bars older than `max_minutes` are pruned from both tiers on
    every append.
    """

    def __init__(self, con_id: int, *, max_minutes: int = 75):
        self.con_id = con_id
        self.max_minutes = max_minutes
        self._backfill_bars: list[_AggregatedBar] = []
        self._live_bars: deque[_StreamingBar] = deque()
        # Latest 5s timestamp seen — used to compute the prune cutoff.
        self._latest_seen: datetime | None = None

    # ----- ingest -----

    def append(self, bar: _StreamingBar) -> None:
        """Add a single 5s bar from the live subscription."""
        self._live_bars.append(bar)
        self._latest_seen = max(self._latest_seen or bar.time, bar.time)
        self._prune()

    def append_backfill(self, one_min_bars: Iterable[Any]) -> None:
        """Seed the backfill tier from `reqHistoricalData` bars.

        Accepts anything with `.date`, `.open`, `.high`, `.low`,
        `.close`, `.volume` — i.e. ib_async `BarData` works directly.
        """
        for b in one_min_bars:
            try:
                self._backfill_bars.append(
                    _AggregatedBar(
                        date=_to_utc(b.date),
                        open=float(b.open),
                        high=float(b.high),
                        low=float(b.low),
                        close=float(b.close),
                        volume=float(getattr(b, "volume", 0) or 0),
                    )
                )
            except Exception as exc:
                log.debug("Skipping malformed backfill bar: %s", exc)
        # Sort + update latest seen.
        self._backfill_bars.sort(key=lambda b: b.date)
        if self._backfill_bars:
            last = self._backfill_bars[-1].date
            self._latest_seen = max(self._latest_seen or last, last)
        self._prune()

    def _prune(self) -> None:
        if self._latest_seen is None:
            return
        cutoff = self._latest_seen.timestamp() - self.max_minutes * 60
        # Live tier.
        while self._live_bars and self._live_bars[0].time.timestamp() < cutoff:
            self._live_bars.popleft()
        # Backfill tier.
        self._backfill_bars = [
            b for b in self._backfill_bars if b.date.timestamp() >= cutoff
        ]

    # ----- output -----

    def to_1min_bars(self) -> list[_AggregatedBar]:
        """Chronological 1-min bars combining backfill + aggregated live.

        Live 5s bars are grouped by minute. A backfill bar at the same
        minute as live data is dropped in favor of the (more recent)
        live aggregate.
        """
        live_aggregated = _aggregate_5s_to_1m(self._live_bars)
        live_minutes = {b.date for b in live_aggregated}
        fresh_backfill = [
            b for b in self._backfill_bars if b.date not in live_minutes
        ]
        merged = sorted(
            (*fresh_backfill, *live_aggregated), key=lambda b: b.date
        )
        return merged

    # ----- inspection -----

    def __len__(self) -> int:
        return len(self._backfill_bars) + len(self._live_bars)

    @property
    def n_live_5s_bars(self) -> int:
        return len(self._live_bars)

    @property
    def n_backfill_bars(self) -> int:
        return len(self._backfill_bars)


def _aggregate_5s_to_1m(bars: Iterable[_StreamingBar]) -> list[_AggregatedBar]:
    """Group 5s bars by their containing minute and emit OHLCV per minute.

    Each minute's bar:
      open  = first bar's open (chronologically)
      close = last bar's close
      high  = max(highs)
      low   = min(lows)
      volume = sum(volumes)
    """
    buckets: dict[datetime, list[_StreamingBar]] = defaultdict(list)
    for b in bars:
        buckets[_minute_bucket(b.time)].append(b)
    out: list[_AggregatedBar] = []
    for minute in sorted(buckets):
        group = buckets[minute]
        group.sort(key=lambda x: x.time)
        out.append(
            _AggregatedBar(
                date=minute,
                open=group[0].open,
                high=max(x.high for x in group),
                low=min(x.low for x in group),
                close=group[-1].close,
                volume=sum(x.volume for x in group),
            )
        )
    return out


# ---- session manager ----


@dataclass
class _Subscription:
    cand: Candidate
    bar_list: Any  # ib_async RealTimeBarList
    callback: Any  # the bound _on_bar_update closure (for detach)


class StreamingSession:
    """Manages per-symbol streaming subscriptions over the watch loop.

    Use as `async with` or call `await close()` manually at shutdown.
    """

    def __init__(self, ib: IB, *, max_subscriptions: int = 100):
        self.ib = ib
        self.max_subscriptions = max_subscriptions
        self._buffers: dict[int, BarBuffer] = {}
        self._subs: dict[int, _Subscription] = {}

    # ----- lifecycle -----

    async def __aenter__(self) -> StreamingSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        for con_id in list(self._subs):
            await self.remove_candidate(con_id)
        log.info("StreamingSession closed; cancelled all subscriptions.")

    # ----- candidate management -----

    async def add_candidate(
        self,
        cand: Candidate,
        *,
        backfill_seconds: int = 4500,
        use_rth: bool = False,
    ) -> None:
        if cand.con_id in self._subs:
            return
        if len(self._subs) >= self.max_subscriptions:
            log.warning(
                "Max streaming subscriptions reached (%d); skipping %s",
                self.max_subscriptions,
                cand.symbol,
            )
            return

        from ib_async import Contract

        contract = Contract(
            conId=cand.con_id,
            secType="STK",
            exchange="SMART",
            currency=cand.currency or "USD",
        )

        # 1) Backfill the buffer with 1-min historical bars.
        buf = BarBuffer(cand.con_id)
        try:
            hist = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=f"{backfill_seconds} S",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=use_rth,
                formatDate=2,
            )
            buf.append_backfill(hist or [])
            log.debug(
                "Backfilled %s with %d 1-min bars",
                cand.symbol,
                buf.n_backfill_bars,
            )
        except Exception as exc:
            log.warning(
                "Backfill failed for %s; subscribing without history: %s",
                cand.symbol,
                exc,
            )

        # 2) Subscribe to live 5s bars.
        try:
            bar_list = self.ib.reqRealTimeBars(
                contract,
                barSize=5,
                whatToShow="TRADES",
                useRTH=use_rth,
            )
        except Exception as exc:
            log.warning("Failed to start real-time bars for %s: %s", cand.symbol, exc)
            return

        # 3) Wire up the event callback (must be a method that closes
        # over `buf` so it doesn't depend on dict lookup speed).
        def _on_bar_update(bars, hasNewBar, _buf=buf):  # noqa: N803
            if not hasNewBar or not bars:
                return
            latest = bars[-1]
            try:
                # ib_async uses `open_` (with trailing underscore) because
                # `open` would shadow the builtin; fall back to `open` for
                # any object that doesn't have the underscored form.
                if hasattr(latest, "open_"):
                    open_val = latest.open_
                else:
                    open_val = latest.open
                _buf.append(
                    _StreamingBar(
                        time=_to_utc(latest.time),
                        open=float(open_val),
                        high=float(latest.high),
                        low=float(latest.low),
                        close=float(latest.close),
                        volume=float(getattr(latest, "volume", 0) or 0),
                    )
                )
            except Exception as exc:
                log.debug("Skipping malformed live bar for %s: %s", cand.symbol, exc)

        bar_list.updateEvent += _on_bar_update

        self._buffers[cand.con_id] = buf
        self._subs[cand.con_id] = _Subscription(
            cand=cand, bar_list=bar_list, callback=_on_bar_update
        )
        log.info(
            "Subscribed to %s (con_id=%d, active=%d/%d)",
            cand.symbol,
            cand.con_id,
            len(self._subs),
            self.max_subscriptions,
        )

    async def remove_candidate(self, con_id: int) -> None:
        sub = self._subs.pop(con_id, None)
        if sub is None:
            return
        try:
            sub.bar_list.updateEvent -= sub.callback
        except Exception:
            pass
        try:
            self.ib.cancelRealTimeBars(sub.bar_list)
        except Exception as exc:
            log.debug("cancelRealTimeBars failed for con_id=%s: %s", con_id, exc)
        self._buffers.pop(con_id, None)
        log.info(
            "Unsubscribed con_id=%d (active=%d)", con_id, len(self._subs)
        )

    async def sync_candidate_pool(
        self,
        candidates: list[Candidate],
        *,
        backfill_seconds: int = 4500,
    ) -> tuple[int, int]:
        """Reconcile current subscriptions with the new candidate set.

        Returns (added, removed). Calls `add_candidate` for new ones
        and `remove_candidate` for dropouts.
        """
        target = {c.con_id: c for c in candidates}
        current = set(self._subs)
        to_remove = current - set(target)
        to_add = [c for c in candidates if c.con_id not in current]

        for con_id in to_remove:
            await self.remove_candidate(con_id)
        for cand in to_add:
            await self.add_candidate(
                cand, backfill_seconds=backfill_seconds
            )
        return len(to_add), len(to_remove)

    # ----- inspection -----

    def get_bars(self, con_id: int) -> list[_AggregatedBar]:
        buf = self._buffers.get(con_id)
        if buf is None:
            return []
        return buf.to_1min_bars()

    def get_candidate(self, con_id: int) -> Candidate | None:
        sub = self._subs.get(con_id)
        return sub.cand if sub else None

    def active_con_ids(self) -> set[int]:
        return set(self._subs)
