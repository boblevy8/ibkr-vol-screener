"""Matplotlib visualisations for snapshots and snapshot history.

Two plot kinds:
  * `plot_snapshot_grid(snap, ...)` — small-multiples grid of the 60-min
    close price for the top-K rows of one snapshot. Useful for eyeballing
    "is this name actually moving or is it noise?"
  * `plot_metric_history(history, ...)` — multi-cycle line plot of a
    chosen metric for the top-K symbols by appearance count in a
    snapshots directory.

Matplotlib is imported lazily so the rest of the CLI works without it
installed. The `chart` CLI command prints a clean install hint when
matplotlib is missing.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any

from .analytics import _rank_value, load_history
from .metrics import compute_metrics
from .snapshot import LoadedSnapshot, load_snapshot

log = logging.getLogger(__name__)


class MatplotlibMissing(RuntimeError):
    """Raised when a chart function is called without matplotlib installed."""


def _import_mpl():
    try:
        import matplotlib

        matplotlib.use("Agg")  # safe default; save_figure still works
        import matplotlib.pyplot as plt  # noqa: F401  (we return it)

        return matplotlib, plt
    except ImportError as exc:
        raise MatplotlibMissing(
            "matplotlib is required for the `chart` command. "
            "Install with: pip install ibkr-vol-screener[charts]"
        ) from exc


def _ranked_rows_from_snapshot(
    snap: LoadedSnapshot, *, sort_key: str, top_k: int
):
    """Recompute metrics for every candidate with bars; return top-K rows
    sorted descending by `sort_key`. Used by `plot_snapshot_grid`."""
    rows = []
    for cand in snap.candidates:
        bars = snap.bars_by_conid.get(cand.con_id, [])
        if not bars:
            continue
        row = compute_metrics(cand, bars, min_bars=10,
                              prior_close=snap.prior_closes.get(cand.con_id))
        if row is not None:
            rows.append((row, bars))
    rows.sort(key=lambda rb: _rank_value(rb[0], sort_key), reverse=True)
    return rows[:top_k]


def plot_snapshot_grid(
    snap_path: Path,
    *,
    top_k: int = 8,
    sort_key: str = "range_pct",
):
    """Render a 2×ceil(top_k/2) grid of close-price line plots."""
    _, plt = _import_mpl()
    snap = load_snapshot(snap_path)
    top_rows = _ranked_rows_from_snapshot(snap, sort_key=sort_key, top_k=top_k)
    if not top_rows:
        raise ValueError(f"No rows with bars in snapshot {snap_path}")

    cols = 2
    rows = (len(top_rows) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 4.5, rows * 2.5))
    if rows == 1 and cols == 1:
        axes_flat = [axes]
    else:
        axes_flat = list(axes.flatten()) if hasattr(axes, "flatten") else list(axes)

    for ax, (row, bars) in zip(axes_flat, top_rows, strict=False):
        closes = [b.close for b in bars]
        ax.plot(range(len(closes)), closes, color="#1a73e8", linewidth=1.4)
        title = f"{row.symbol}  {sort_key}={getattr(row, sort_key):.2f}"
        ax.set_title(title, fontsize=9)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.3)
    # Hide unused subplots.
    for ax in axes_flat[len(top_rows):]:
        ax.set_visible(False)

    fig.suptitle(
        f"Snapshot {snap_path.name} — top {len(top_rows)} by {sort_key}",
        fontsize=11,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return fig


def plot_metric_history(
    snapshots_root: Path,
    *,
    top_k: int = 8,
    metric: str = "range_pct",
    sort_key: str = "range_pct",
):
    """Multi-cycle line plot of `metric` for the top-K most-frequent
    symbols (ranked by appearance count in each cycle's top-K-by-sort_key
    list)."""
    _, plt = _import_mpl()
    history = load_history(snapshots_root)
    if not history:
        raise ValueError(f"No snapshots found under {snapshots_root}")

    # Find top-K most-frequent symbols (by appearances in each cycle's
    # top-K-by-sort_key).
    appearances: Counter[str] = Counter()
    for _, rows in history:
        ranked = sorted(rows, key=lambda r: _rank_value(r, sort_key), reverse=True)[:top_k]
        for r in ranked:
            appearances[r.symbol] += 1
    top_symbols = [s for s, _ in appearances.most_common(top_k)]
    if not top_symbols:
        raise ValueError("No symbols qualified for the history plot")

    # Per-symbol (ts, value) series.
    series: dict[str, list[tuple[Any, float]]] = {s: [] for s in top_symbols}
    for ts, rows in history:
        by_sym = {r.symbol: r for r in rows}
        for sym in top_symbols:
            r = by_sym.get(sym)
            if r is None:
                continue
            v = getattr(r, metric, None)
            if v is None:
                continue
            series[sym].append((ts, float(v)))

    fig, ax = plt.subplots(figsize=(10, 5.5))
    for sym in top_symbols:
        pts = series[sym]
        if not pts:
            continue
        xs, ys = zip(*pts, strict=True)
        ax.plot(xs, ys, marker="o", markersize=3, label=sym, linewidth=1.4)
    ax.set_title(
        f"{metric} over {len(history)} cycles — top {len(top_symbols)} by {sort_key}",
        fontsize=11,
    )
    ax.set_xlabel("Snapshot timestamp")
    ax.set_ylabel(metric)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=8, ncol=2)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def save_figure(fig, path: Path, *, dpi: int = 110) -> None:
    """Save a figure as PNG / SVG / PDF based on path suffix."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    log.info("Wrote chart -> %s", path)
