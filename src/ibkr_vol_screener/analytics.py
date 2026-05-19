"""Walk a directory of saved snapshots and compute cross-cycle analytics.

Inputs: a root directory whose child subdirectories each contain a
`snapshot.json` (the format that `snapshot.save_snapshot` produces).
Output: structured summaries that the `analyze` CLI command renders as
Rich tables and an optional long-form CSV.

This module is read-only by construction: it never imports `ib_client`
or calls any IBKR endpoint. All inputs come from disk via
`snapshot.load_snapshot`.
"""

from __future__ import annotations

import csv
import logging
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .metrics import ScreenRow, compute_metrics
from .snapshot import load_snapshot

log = logging.getLogger(__name__)


# -------- data classes --------


@dataclass
class HistoryPoint:
    ts: datetime
    row: ScreenRow


@dataclass
class SymbolSeries:
    symbol: str
    bucket: str
    points: list[HistoryPoint] = field(default_factory=list)

    def values_of(self, metric: str) -> list[float]:
        out: list[float] = []
        for p in self.points:
            v = getattr(p.row, metric, None)
            if v is not None:
                out.append(float(v))
        return out


@dataclass
class HistoryOverview:
    cycles: int
    first_ts: datetime | None
    last_ts: datetime | None
    total_unique_symbols: int


@dataclass
class ChurnEntry:
    symbol: str
    bucket: str
    appearances: int
    fraction: float
    first_seen: datetime
    last_seen: datetime


@dataclass
class ChurnSummary:
    k: int
    sort_key: str
    cycles: int
    entries: list[ChurnEntry]


@dataclass
class ScanCodePredictiveness:
    metric: str
    by_code: list[tuple[str, float, int]]  # (scan_code, mean_metric, n_rows)


# -------- loading + recomputing --------


def _iter_snapshot_dirs(root: Path) -> Iterable[Path]:
    """Yield direct children of `root` that contain a `snapshot.json`.

    Sorted by directory name so an ISO-timestamped naming scheme yields
    chronological order; we still re-sort by captured_at for safety.
    """
    if not root.exists() or not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "snapshot.json").exists())


def load_history(root: Path) -> list[tuple[datetime, list[ScreenRow]]]:
    """Walk `root` for snapshot dirs, return chronological [(ts, rows), ...].

    Each `rows` is the metric-recomputed set from the bars saved in that
    snapshot. Rows with too few bars to compute metrics are skipped.
    """
    out: list[tuple[datetime, list[ScreenRow]]] = []
    for d in _iter_snapshot_dirs(root):
        try:
            snap = load_snapshot(d)
        except Exception as exc:
            log.warning("Skipping %s: %s", d, exc)
            continue
        ts = _parse_ts(snap.captured_at) or datetime.fromtimestamp(d.stat().st_mtime).astimezone()
        rows: list[ScreenRow] = []
        for cand in snap.candidates:
            bars = snap.bars_by_conid.get(cand.con_id, [])
            if not bars:
                continue
            row = compute_metrics(
                cand,
                bars,
                min_bars=10,
                prior_close=snap.prior_closes.get(cand.con_id),
            )
            if row is not None:
                row.what_to_show = snap.what_to_show_by_conid.get(cand.con_id, "TRADES")
                rows.append(row)
        out.append((ts, rows))
    out.sort(key=lambda x: x[0])
    log.info("Loaded history: %d cycles from %s", len(out), root)
    return out


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# -------- summaries --------


def overview(history: list[tuple[datetime, list[ScreenRow]]]) -> HistoryOverview:
    if not history:
        return HistoryOverview(cycles=0, first_ts=None, last_ts=None, total_unique_symbols=0)
    symbols = {r.symbol for _, rows in history for r in rows}
    return HistoryOverview(
        cycles=len(history),
        first_ts=history[0][0],
        last_ts=history[-1][0],
        total_unique_symbols=len(symbols),
    )


def per_symbol_series(
    history: list[tuple[datetime, list[ScreenRow]]],
) -> dict[str, SymbolSeries]:
    by_sym: dict[str, SymbolSeries] = {}
    for ts, rows in history:
        for r in rows:
            ss = by_sym.get(r.symbol)
            if ss is None:
                ss = SymbolSeries(symbol=r.symbol, bucket=r.bucket)
                by_sym[r.symbol] = ss
            ss.points.append(HistoryPoint(ts=ts, row=r))
    return by_sym


def _signed_magnitude(key: str) -> bool:
    return key in {"vwap_dev_pct", "gap_pct"}


def _rank_value(row: ScreenRow, key: str) -> float:
    v = getattr(row, key, None)
    if v is None:
        return float("-inf")
    return abs(float(v)) if _signed_magnitude(key) else float(v)


def compute_top_k_churn(
    history: list[tuple[datetime, list[ScreenRow]]],
    *,
    k: int = 10,
    sort_key: str = "range_pct",
) -> ChurnSummary:
    """For each cycle take the top-K by `sort_key`; count appearances per symbol."""
    appearances: Counter[str] = Counter()
    bucket_of: dict[str, str] = {}
    first_seen: dict[str, datetime] = {}
    last_seen: dict[str, datetime] = {}
    for ts, rows in history:
        if not rows:
            continue
        ranked = sorted(rows, key=lambda r: _rank_value(r, sort_key), reverse=True)[:k]
        for r in ranked:
            appearances[r.symbol] += 1
            bucket_of.setdefault(r.symbol, r.bucket)
            first_seen.setdefault(r.symbol, ts)
            last_seen[r.symbol] = ts
    cycles = len(history)
    entries = [
        ChurnEntry(
            symbol=sym,
            bucket=bucket_of[sym],
            appearances=n,
            fraction=(n / cycles) if cycles else 0.0,
            first_seen=first_seen[sym],
            last_seen=last_seen[sym],
        )
        for sym, n in appearances.most_common()
    ]
    return ChurnSummary(k=k, sort_key=sort_key, cycles=cycles, entries=entries)


def scan_code_predictiveness(
    history: list[tuple[datetime, list[ScreenRow]]],
    *,
    metric: str = "range_pct",
) -> ScanCodePredictiveness:
    """For each source scan code, mean value of `metric` across rows whose
    source_scan_codes include that code. Higher = the scanner surfaced
    names that actually turned out volatile (under our metric)."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for _, rows in history:
        for r in rows:
            v = getattr(r, metric, None)
            if v is None:
                continue
            value = abs(float(v)) if _signed_magnitude(metric) else float(v)
            for code in r.source_scan_codes:
                buckets[code].append(value)
    by_code = [
        (code, statistics.fmean(vals), len(vals))
        for code, vals in buckets.items()
        if vals
    ]
    by_code.sort(key=lambda x: x[1], reverse=True)
    return ScanCodePredictiveness(metric=metric, by_code=by_code)


# -------- CSV export --------


def write_long_form_csv(
    history: list[tuple[datetime, list[ScreenRow]]],
    path: Path,
    metrics: tuple[str, ...] = (
        "range_pct",
        "abs_return_pct",
        "atr_pct_60m",
        "vwap_dev_pct",
        "realized_vol_60m",
        "volume_60m",
        "dollar_volume_60m",
        "gap_pct",
    ),
) -> None:
    """Write (ts, symbol, bucket, metric, value) rows for downstream tools."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "symbol", "bucket", "metric", "value"])
        for ts, rows in history:
            iso = ts.isoformat()
            for r in rows:
                for m in metrics:
                    v = getattr(r, m, None)
                    if v is None:
                        continue
                    w.writerow([iso, r.symbol, r.bucket, m, v])
    log.info("Wrote long-form history CSV -> %s", path)


# -------- rich rendering --------


def render_overview_table(ov: HistoryOverview):
    from rich.table import Table

    t = Table(title="Snapshot history overview", show_lines=False)
    t.add_column("Field", style="bold")
    t.add_column("Value")
    t.add_row("Cycles", str(ov.cycles))
    t.add_row("First seen", ov.first_ts.isoformat() if ov.first_ts else "-")
    t.add_row("Last seen", ov.last_ts.isoformat() if ov.last_ts else "-")
    t.add_row("Unique symbols", str(ov.total_unique_symbols))
    return t


def render_churn_table(summary: ChurnSummary, *, limit: int = 25):
    from rich.table import Table

    t = Table(
        title=f"Top-{summary.k} churn by {summary.sort_key} (across {summary.cycles} cycles)",
        show_lines=False,
    )
    t.add_column("Sym", style="bold cyan")
    t.add_column("Bkt", style="magenta")
    t.add_column("Appearances", justify="right")
    t.add_column("Fraction", justify="right")
    t.add_column("First seen")
    t.add_column("Last seen")
    for entry in summary.entries[:limit]:
        t.add_row(
            entry.symbol,
            entry.bucket,
            str(entry.appearances),
            f"{entry.fraction:.0%}",
            entry.first_seen.isoformat(timespec="minutes"),
            entry.last_seen.isoformat(timespec="minutes"),
        )
    return t


def render_persistent_series_table(
    by_sym: dict[str, SymbolSeries],
    *,
    cycles: int,
    metric: str = "range_pct",
    min_fraction: float = 0.25,
    limit: int = 25,
):
    """Per-symbol min/median/max of `metric` for symbols present in
    at least `min_fraction` of all cycles."""
    from rich.table import Table

    from .reporting import sparkline

    rows: list[tuple[str, str, int, float, float, float, list[float]]] = []
    for sym, ss in by_sym.items():
        if cycles and (len(ss.points) / cycles) < min_fraction:
            continue
        vals = ss.values_of(metric)
        if not vals:
            continue
        rows.append(
            (
                sym,
                ss.bucket,
                len(ss.points),
                min(vals),
                statistics.median(vals),
                max(vals),
                vals,
            )
        )
    rows.sort(key=lambda r: r[5], reverse=True)  # by max

    t = Table(
        title=f"Persistent symbols (>= {int(min_fraction*100)}% of cycles) by {metric}",
        show_lines=False,
    )
    t.add_column("Sym", style="bold cyan")
    t.add_column("Bkt", style="magenta")
    t.add_column("Cycles", justify="right")
    t.add_column("Min", justify="right")
    t.add_column("Median", justify="right")
    t.add_column("Max", justify="right", style="bold yellow")
    t.add_column("Shape (over time)", justify="left")
    for sym, bkt, n, mn, med, mx, vals in rows[:limit]:
        t.add_row(
            sym, bkt, str(n),
            f"{mn:.2f}", f"{med:.2f}", f"{mx:.2f}",
            sparkline(vals, width=10),
        )
    return t


def render_scan_predictiveness_table(pred: ScanCodePredictiveness):
    from rich.table import Table

    t = Table(
        title=f"Scan-code predictiveness (mean {pred.metric})",
        show_lines=False,
    )
    t.add_column("Scan code", style="bold green")
    t.add_column("Mean", justify="right")
    t.add_column("N rows", justify="right")
    for code, mean, n in pred.by_code:
        t.add_row(code, f"{mean:.2f}", str(n))
    return t
