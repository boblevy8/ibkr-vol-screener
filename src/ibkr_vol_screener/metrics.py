"""Pure metric computations over the last 60 minutes of 1-minute bars.

These functions take plain `BarLike` objects (anything with `date`, `open`,
`high`, `low`, `close`, `volume`) and produce a `ScreenRow`. They are
deliberately independent of `ib_async` so they can be unit-tested with
synthetic bar fixtures.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .config import MarketBucket
from .scanners import Candidate


class BarLike(Protocol):
    date: Any
    open: float
    high: float
    low: float
    close: float
    volume: float


# Annualization factor for 1-min returns over a US equity day:
# 252 trading days × 390 minutes per regular session.
_ANNUALIZE_MIN = math.sqrt(252.0 * 390.0)


@dataclass
class ScreenRow:
    symbol: str
    con_id: int
    bucket: str
    exchange: str
    primary_exchange: str
    currency: str
    first_bar_time: datetime
    last_bar_time: datetime
    n_bars: int
    first_open: float
    last_close: float
    high_60m: float
    low_60m: float
    range_pct: float
    signed_return_pct: float
    abs_return_pct: float
    volume_60m: float
    dollar_volume_60m: float
    realized_vol_60m: float
    source_scan_codes: tuple[str, ...]
    what_to_show: str = "TRADES"
    # Added in Phase 2:
    vwap_60m: float = 0.0
    vwap_dev_pct: float = 0.0
    atr_pct_60m: float = 0.0
    # gap_pct is None unless the caller supplied prior_close (gated by --with-gap).
    gap_pct: float | None = None
    # Sequence of closes within the 60m window — used to render the sparkline
    # column and to plot the price shape in CSV/JSON/HTML exports.
    closes_60m: tuple[float, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["first_bar_time"] = self.first_bar_time.isoformat()
        d["last_bar_time"] = self.last_bar_time.isoformat()
        d["source_scan_codes"] = list(self.source_scan_codes)
        return d


def _to_datetime(value: Any) -> datetime:
    """Normalize ib_async bar.date (datetime | date | int epoch | str) to UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        # IBKR string format: "20240131  14:30:00" or ISO.
        s = value.strip().replace("  ", " ")
        for fmt in ("%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    # date or unknown -> midnight UTC fallback (won't pass the 60m window).
    try:
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    except Exception as exc:  # pragma: no cover - defensive
        raise TypeError(f"Cannot normalize bar.date={value!r}") from exc


def slice_last_60min(bars: list[BarLike], *, now: datetime | None = None) -> list[BarLike]:
    """Return bars whose timestamp is within the last 60 minutes of `now`
    (or now=the latest bar in the series if not provided)."""
    if not bars:
        return []
    times = [_to_datetime(b.date) for b in bars]
    if now is None:
        now = max(times)
    cutoff = now - timedelta(minutes=60)
    return [b for b, t in zip(bars, times, strict=False) if t >= cutoff]


def compute_metrics(
    candidate: Candidate,
    bars: list[BarLike],
    *,
    min_bars: int = 10,
    now: datetime | None = None,
    prior_close: float | None = None,
) -> ScreenRow | None:
    """Compute the screen metrics for one candidate.

    Returns None when there are fewer than `min_bars` bars in the 60-minute
    window or when the data is degenerate (e.g. zero last_close).

    `prior_close` (optional) is the previous session's close used to compute
    `gap_pct`. Callers pass it only when the user opted into the extra
    historical request via `--with-gap`.
    """
    window = slice_last_60min(bars, now=now)
    if len(window) < min_bars:
        return None

    times = [_to_datetime(b.date) for b in window]
    closes = [float(b.close) for b in window]
    opens = [float(b.open) for b in window]
    highs = [float(b.high) for b in window]
    lows = [float(b.low) for b in window]
    volumes = [float(getattr(b, "volume", 0) or 0) for b in window]

    last_close = closes[-1]
    first_open = opens[0]
    high_60m = max(highs)
    low_60m = min(lows)

    if last_close <= 0 or first_open <= 0:
        return None

    range_pct = (high_60m - low_60m) / last_close * 100.0
    signed_return_pct = (last_close - first_open) / first_open * 100.0
    abs_return_pct = abs(signed_return_pct)

    volume_60m = sum(volumes)
    dollar_volume_60m = sum(c * v for c, v in zip(closes, volumes, strict=True))

    # Realized vol from 1-min log returns, annualized.
    log_returns: list[float] = []
    for prev, curr in zip(closes[:-1], closes[1:], strict=True):
        if prev > 0 and curr > 0:
            log_returns.append(math.log(curr / prev))
    if log_returns:
        var = sum(r * r for r in log_returns) / len(log_returns)
        realized_vol_60m = math.sqrt(var) * _ANNUALIZE_MIN * 100.0
    else:
        realized_vol_60m = 0.0

    # VWAP over the 60-minute window using typical price (H+L+C)/3 per bar.
    typical_x_vol = [
        ((h + low + c) / 3.0) * v
        for h, low, c, v in zip(highs, lows, closes, volumes, strict=True)
    ]
    sum_v = sum(volumes)
    vwap_60m = (sum(typical_x_vol) / sum_v) if sum_v > 0 else 0.0
    vwap_dev_pct = ((last_close - vwap_60m) / vwap_60m * 100.0) if vwap_60m > 0 else 0.0

    # ATR over the 60-minute window. true_range_i uses the previous CLOSE
    # for the gap component; the first bar falls back to the bar's own range.
    true_ranges: list[float] = []
    for i, (h, low, _c) in enumerate(zip(highs, lows, closes, strict=True)):
        if i == 0:
            true_ranges.append(h - low)
        else:
            prev_c = closes[i - 1]
            true_ranges.append(max(h - low, abs(h - prev_c), abs(low - prev_c)))
    atr_60m = sum(true_ranges) / len(true_ranges)
    atr_pct_60m = (atr_60m / last_close * 100.0) if last_close > 0 else 0.0

    gap_pct: float | None = None
    if prior_close is not None and prior_close > 0:
        gap_pct = (opens[0] - prior_close) / prior_close * 100.0

    bucket_value = (
        candidate.bucket.value
        if isinstance(candidate.bucket, MarketBucket)
        else str(candidate.bucket)
    )

    return ScreenRow(
        symbol=candidate.symbol,
        con_id=candidate.con_id,
        bucket=bucket_value,
        exchange=candidate.exchange,
        primary_exchange=candidate.primary_exchange,
        currency=candidate.currency,
        first_bar_time=times[0],
        last_bar_time=times[-1],
        n_bars=len(window),
        first_open=first_open,
        last_close=last_close,
        high_60m=high_60m,
        low_60m=low_60m,
        range_pct=range_pct,
        signed_return_pct=signed_return_pct,
        abs_return_pct=abs_return_pct,
        volume_60m=volume_60m,
        dollar_volume_60m=dollar_volume_60m,
        realized_vol_60m=realized_vol_60m,
        source_scan_codes=tuple(sorted(candidate.source_scan_codes)),
        vwap_60m=vwap_60m,
        vwap_dev_pct=vwap_dev_pct,
        atr_pct_60m=atr_pct_60m,
        gap_pct=gap_pct,
        closes_60m=tuple(closes),
    )
