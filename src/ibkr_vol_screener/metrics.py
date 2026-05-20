"""Pure metric computations over the last 60 minutes of 1-minute bars.

Phase 6 added multi-timeframe sub-windows (5m/15m/30m alongside the
canonical 60m view) and a configurable composite_score that blends
the most-useful signals into a single rankable number.

These functions take plain `BarLike` objects (anything with `date`,
`open`, `high`, `low`, `close`, `volume`) and produce a `ScreenRow`.
They are deliberately independent of `ib_async` so they can be
unit-tested with synthetic bar fixtures.
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


# Default composite-score weights. Tunable via Config.score_weights
# (or the [score] section of the TOML config).
DEFAULT_SCORE_WEIGHTS: dict[str, float] = {
    "range_pct": 0.30,
    "atr_pct_60m": 0.20,
    "vwap_dev_abs": 0.15,
    "accel_factor": 0.25,
    "realized_vol_60m": 0.10,
}

# Upper clips per component so a single extreme value can't dominate
# the score. Picked from observed live values + paranoia.
_SCORE_CLIPS: dict[str, float] = {
    "range_pct": 50.0,
    "atr_pct_60m": 10.0,
    "vwap_dev_abs": 10.0,
    "accel_factor": 4.0,
    "realized_vol_60m": 500.0,
}


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
    # Phase 2:
    vwap_60m: float = 0.0
    vwap_dev_pct: float = 0.0
    atr_pct_60m: float = 0.0
    gap_pct: float | None = None
    # Phase 4: sparkline source.
    closes_60m: tuple[float, ...] = ()
    # Phase 6: shorter-window metrics. Default 0.0 so old snapshots and
    # tests that construct ScreenRow directly keep working.
    range_pct_5m: float = 0.0
    range_pct_15m: float = 0.0
    range_pct_30m: float = 0.0
    atr_pct_5m: float = 0.0
    atr_pct_15m: float = 0.0
    atr_pct_30m: float = 0.0
    realized_vol_5m: float = 0.0
    realized_vol_15m: float = 0.0
    realized_vol_30m: float = 0.0
    vwap_dev_pct_5m: float = 0.0
    vwap_dev_pct_15m: float = 0.0
    vwap_dev_pct_30m: float = 0.0
    signed_return_pct_5m: float = 0.0
    signed_return_pct_15m: float = 0.0
    signed_return_pct_30m: float = 0.0
    accel_factor: float = 0.0
    composite_score: float = 0.0

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
    # date or unknown -> midnight UTC fallback (won't pass any window).
    try:
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    except Exception as exc:  # pragma: no cover - defensive
        raise TypeError(f"Cannot normalize bar.date={value!r}") from exc


def _slice_window(
    bars: list[BarLike], *, window_minutes: int, now: datetime | None
) -> list[BarLike]:
    """Return bars whose timestamp is within the last `window_minutes` of `now`
    (or the latest bar's time if `now` is None)."""
    if not bars:
        return []
    times = [_to_datetime(b.date) for b in bars]
    if now is None:
        now = max(times)
    cutoff = now - timedelta(minutes=window_minutes)
    return [b for b, t in zip(bars, times, strict=False) if t >= cutoff]


# Backwards-compatible wrapper used by existing tests + analytics module.
def slice_last_60min(bars: list[BarLike], *, now: datetime | None = None) -> list[BarLike]:
    return _slice_window(bars, window_minutes=60, now=now)


@dataclass(frozen=True)
class _WindowMetrics:
    """Per-window metric bundle. Internal; not part of the public API."""

    n_bars: int
    first_open: float
    last_close: float
    high: float
    low: float
    range_pct: float
    signed_return_pct: float
    abs_return_pct: float
    realized_vol: float
    atr_pct: float
    vwap: float
    vwap_dev_pct: float
    volume: float
    dollar_volume: float
    closes: tuple[float, ...] = ()


def _compute_window_metrics(window: list[BarLike]) -> _WindowMetrics | None:
    """Compute the metric bundle for a single window. Returns None when the
    window is empty or degenerate."""
    if not window:
        return None

    closes = [float(b.close) for b in window]
    opens = [float(b.open) for b in window]
    highs = [float(b.high) for b in window]
    lows = [float(b.low) for b in window]
    volumes = [float(getattr(b, "volume", 0) or 0) for b in window]

    last_close = closes[-1]
    first_open = opens[0]
    if last_close <= 0 or first_open <= 0:
        return None
    high = max(highs)
    low = min(lows)

    range_pct = (high - low) / last_close * 100.0
    signed_return_pct = (last_close - first_open) / first_open * 100.0
    abs_return_pct = abs(signed_return_pct)

    volume = sum(volumes)
    dollar_volume = sum(c * v for c, v in zip(closes, volumes, strict=True))

    log_returns: list[float] = []
    for prev, curr in zip(closes[:-1], closes[1:], strict=True):
        if prev > 0 and curr > 0:
            log_returns.append(math.log(curr / prev))
    if log_returns:
        var = sum(r * r for r in log_returns) / len(log_returns)
        realized_vol = math.sqrt(var) * _ANNUALIZE_MIN * 100.0
    else:
        realized_vol = 0.0

    typical_x_vol = [
        ((h + lo + c) / 3.0) * v
        for h, lo, c, v in zip(highs, lows, closes, volumes, strict=True)
    ]
    sum_v = volume
    vwap = (sum(typical_x_vol) / sum_v) if sum_v > 0 else 0.0
    vwap_dev_pct = ((last_close - vwap) / vwap * 100.0) if vwap > 0 else 0.0

    true_ranges: list[float] = []
    for i, (h, lo, _c) in enumerate(zip(highs, lows, closes, strict=True)):
        if i == 0:
            true_ranges.append(h - lo)
        else:
            prev_c = closes[i - 1]
            true_ranges.append(max(h - lo, abs(h - prev_c), abs(lo - prev_c)))
    atr_abs = sum(true_ranges) / len(true_ranges)
    atr_pct = (atr_abs / last_close * 100.0) if last_close > 0 else 0.0

    return _WindowMetrics(
        n_bars=len(window),
        first_open=first_open,
        last_close=last_close,
        high=high,
        low=low,
        range_pct=range_pct,
        signed_return_pct=signed_return_pct,
        abs_return_pct=abs_return_pct,
        realized_vol=realized_vol,
        atr_pct=atr_pct,
        vwap=vwap,
        vwap_dev_pct=vwap_dev_pct,
        volume=volume,
        dollar_volume=dollar_volume,
        closes=tuple(closes),
    )


def _clip(name: str, value: float) -> float:
    return min(abs(value), _SCORE_CLIPS.get(name, 1e9))


def compute_composite_score(row: ScreenRow, weights: dict[str, float]) -> float:
    """Weighted, clipped blend of the most-useful metrics into one number."""
    components = {
        "range_pct": row.range_pct,
        "atr_pct_60m": row.atr_pct_60m,
        "vwap_dev_abs": abs(row.vwap_dev_pct),
        "accel_factor": row.accel_factor,
        "realized_vol_60m": row.realized_vol_60m,
    }
    total = 0.0
    for key, weight in weights.items():
        if key not in components:
            continue  # silently skip unknown keys; they're caught at TOML load
        total += float(weight) * _clip(key, float(components[key]))
    return total


def compute_metrics(
    candidate: Candidate,
    bars: list[BarLike],
    *,
    min_bars: int = 10,
    min_bars_short: int = 3,
    now: datetime | None = None,
    prior_close: float | None = None,
    score_weights: dict[str, float] | None = None,
) -> ScreenRow | None:
    """Compute the multi-timeframe metrics + composite score for one candidate.

    Returns None when the 60m window has fewer than `min_bars` bars or the
    data is degenerate. Shorter windows with fewer than `min_bars_short`
    bars leave their fields at 0.0 rather than crashing.

    `prior_close` populates `gap_pct` when supplied (gated by --with-gap).
    `score_weights` overrides DEFAULT_SCORE_WEIGHTS.
    """
    weights = score_weights or DEFAULT_SCORE_WEIGHTS

    window_60 = _slice_window(bars, window_minutes=60, now=now)
    if len(window_60) < min_bars:
        return None
    wm60 = _compute_window_metrics(window_60)
    if wm60 is None:
        return None

    # Times needed for first/last_bar_time.
    times_60 = [_to_datetime(b.date) for b in window_60]

    # Sub-windows. Each falls back to 0-valued fields when too few bars.
    sub_windows: dict[int, _WindowMetrics | None] = {}
    for win_min in (5, 15, 30):
        sub = _slice_window(bars, window_minutes=win_min, now=now)
        if len(sub) >= min_bars_short:
            sub_windows[win_min] = _compute_window_metrics(sub)
        else:
            sub_windows[win_min] = None

    def _sub_or_zero(win: int, attr: str) -> float:
        wm = sub_windows.get(win)
        if wm is None:
            return 0.0
        return float(getattr(wm, attr))

    gap_pct: float | None = None
    if prior_close is not None and prior_close > 0:
        gap_pct = (window_60[0].open - prior_close) / prior_close * 100.0

    bucket_value = (
        candidate.bucket.value
        if isinstance(candidate.bucket, MarketBucket)
        else str(candidate.bucket)
    )

    row = ScreenRow(
        symbol=candidate.symbol,
        con_id=candidate.con_id,
        bucket=bucket_value,
        exchange=candidate.exchange,
        primary_exchange=candidate.primary_exchange,
        currency=candidate.currency,
        first_bar_time=times_60[0],
        last_bar_time=times_60[-1],
        n_bars=wm60.n_bars,
        first_open=wm60.first_open,
        last_close=wm60.last_close,
        high_60m=wm60.high,
        low_60m=wm60.low,
        range_pct=wm60.range_pct,
        signed_return_pct=wm60.signed_return_pct,
        abs_return_pct=wm60.abs_return_pct,
        volume_60m=wm60.volume,
        dollar_volume_60m=wm60.dollar_volume,
        realized_vol_60m=wm60.realized_vol,
        source_scan_codes=tuple(sorted(candidate.source_scan_codes)),
        vwap_60m=wm60.vwap,
        vwap_dev_pct=wm60.vwap_dev_pct,
        atr_pct_60m=wm60.atr_pct,
        gap_pct=gap_pct,
        closes_60m=wm60.closes,
        range_pct_5m=_sub_or_zero(5, "range_pct"),
        range_pct_15m=_sub_or_zero(15, "range_pct"),
        range_pct_30m=_sub_or_zero(30, "range_pct"),
        atr_pct_5m=_sub_or_zero(5, "atr_pct"),
        atr_pct_15m=_sub_or_zero(15, "atr_pct"),
        atr_pct_30m=_sub_or_zero(30, "atr_pct"),
        realized_vol_5m=_sub_or_zero(5, "realized_vol"),
        realized_vol_15m=_sub_or_zero(15, "realized_vol"),
        realized_vol_30m=_sub_or_zero(30, "realized_vol"),
        vwap_dev_pct_5m=_sub_or_zero(5, "vwap_dev_pct"),
        vwap_dev_pct_15m=_sub_or_zero(15, "vwap_dev_pct"),
        vwap_dev_pct_30m=_sub_or_zero(30, "vwap_dev_pct"),
        signed_return_pct_5m=_sub_or_zero(5, "signed_return_pct"),
        signed_return_pct_15m=_sub_or_zero(15, "signed_return_pct"),
        signed_return_pct_30m=_sub_or_zero(30, "signed_return_pct"),
    )
    # Acceleration: how much of the 60m range happened in the most recent
    # 15 minutes, relative to a flat distribution (0.25). Values > 1 mean
    # the recent 15 minutes outran the hour's average minute by a wide
    # margin. 0 when 60m range is zero (e.g. flat day).
    row.accel_factor = (
        row.range_pct_15m / row.range_pct if row.range_pct > 0 else 0.0
    )
    row.composite_score = compute_composite_score(row, weights)
    return row
