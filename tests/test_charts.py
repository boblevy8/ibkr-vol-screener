"""Tests for the matplotlib-backed chart helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

plt = pytest.importorskip("matplotlib.pyplot")  # skip suite if matplotlib missing

from ibkr_vol_screener.charts import (  # noqa: E402
    plot_metric_history,
    plot_snapshot_grid,
    save_figure,
)
from ibkr_vol_screener.config import Config, MarketBucket  # noqa: E402
from ibkr_vol_screener.historical import BarsResult  # noqa: E402
from ibkr_vol_screener.scanners import Candidate  # noqa: E402
from ibkr_vol_screener.snapshot import save_snapshot  # noqa: E402

from .conftest import make_bars  # noqa: E402


def _cand(con_id: int, sym: str) -> Candidate:
    return Candidate(
        con_id=con_id,
        symbol=sym,
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        bucket=MarketBucket.US_MAJOR,
        source_scan_codes={"HOT_BY_VOLUME"},
    )


def _build_snapshot(root: Path, name: str, *, step_pct: float = 0.1):
    now = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    cands = [_cand(1, "AAA"), _cand(2, "BBB")]
    bars_a = make_bars(now - timedelta(minutes=60), count=60, step_pct=step_pct)
    bars_b = make_bars(now - timedelta(minutes=60), count=60, step_pct=-0.05)
    save_snapshot(
        root / name,
        Config(),
        cands,
        [BarsResult(cands[0], bars=bars_a), BarsResult(cands[1], bars=bars_b)],
    )


def test_plot_snapshot_grid_returns_figure_and_saves(tmp_path: Path):
    _build_snapshot(tmp_path, "cycle-001")
    fig = plot_snapshot_grid(tmp_path / "cycle-001", top_k=2, sort_key="range_pct")
    out = tmp_path / "chart.png"
    save_figure(fig, out)
    assert out.exists()
    assert out.stat().st_size > 1000  # non-trivial PNG
    plt.close(fig)


def test_plot_metric_history_returns_figure(tmp_path: Path):
    _build_snapshot(tmp_path, "cycle-001", step_pct=0.1)
    _build_snapshot(tmp_path, "cycle-002", step_pct=0.2)
    fig = plot_metric_history(
        tmp_path, top_k=2, metric="range_pct", sort_key="range_pct"
    )
    out = tmp_path / "history.svg"
    save_figure(fig, out)
    assert out.exists()
    assert out.stat().st_size > 500  # non-trivial SVG
    plt.close(fig)


def test_plot_snapshot_grid_empty_raises(tmp_path: Path):
    # Snapshot with no bars (zero candidates) -> ValueError.
    save_snapshot(tmp_path / "empty", Config(), [], [])
    with pytest.raises(ValueError, match="No rows"):
        plot_snapshot_grid(tmp_path / "empty", top_k=4, sort_key="range_pct")
