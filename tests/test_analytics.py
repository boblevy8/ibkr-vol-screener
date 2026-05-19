"""Tests for snapshot history analytics."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ibkr_vol_screener.analytics import (
    compute_top_k_churn,
    load_history,
    overview,
    per_symbol_series,
    scan_code_predictiveness,
    write_long_form_csv,
)
from ibkr_vol_screener.config import Config, MarketBucket
from ibkr_vol_screener.historical import BarsResult
from ibkr_vol_screener.scanners import Candidate
from ibkr_vol_screener.snapshot import save_snapshot

from .conftest import make_bars


def _cand(con_id: int, sym: str, *, scans: set[str] = None) -> Candidate:
    return Candidate(
        con_id=con_id,
        symbol=sym,
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        bucket=MarketBucket.US_MAJOR,
        source_scan_codes=scans or {"HOT_BY_VOLUME"},
    )


def _build_two_cycle_history(tmp: Path) -> None:
    """Two snapshots: AAA in both, BBB in only the first."""
    now = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)

    aaa_t0 = make_bars(now - timedelta(minutes=60), count=60, open_=100.0, step_pct=0.1)
    bbb_t0 = make_bars(now - timedelta(minutes=60), count=60, open_=50.0, step_pct=-0.05)
    save_snapshot(
        tmp / "2026-05-19T16-00-00",
        Config(),
        [_cand(1, "AAA", scans={"HOT_BY_VOLUME"}), _cand(2, "BBB", scans={"TOP_PERC_GAIN"})],
        [
            BarsResult(_cand(1, "AAA"), bars=aaa_t0),
            BarsResult(_cand(2, "BBB"), bars=bbb_t0),
        ],
    )
    # Wait so dir listing is stable; second cycle a minute later.
    time.sleep(0.01)
    aaa_t1 = make_bars(now - timedelta(minutes=60), count=60, open_=110.0, step_pct=0.2)
    save_snapshot(
        tmp / "2026-05-19T16-01-00",
        Config(),
        [_cand(1, "AAA", scans={"HOT_BY_VOLUME", "MOST_ACTIVE"})],
        [BarsResult(_cand(1, "AAA"), bars=aaa_t1)],
    )


def test_load_history_chronological(tmp_path: Path):
    _build_two_cycle_history(tmp_path)
    history = load_history(tmp_path)
    assert len(history) == 2
    assert history[0][0] < history[1][0]
    # First cycle has both symbols.
    first_syms = {r.symbol for r in history[0][1]}
    assert first_syms == {"AAA", "BBB"}
    second_syms = {r.symbol for r in history[1][1]}
    assert second_syms == {"AAA"}


def test_overview_counts(tmp_path: Path):
    _build_two_cycle_history(tmp_path)
    history = load_history(tmp_path)
    ov = overview(history)
    assert ov.cycles == 2
    assert ov.total_unique_symbols == 2
    assert ov.first_ts is not None
    assert ov.last_ts is not None
    assert ov.last_ts > ov.first_ts


def test_per_symbol_series_groups_correctly(tmp_path: Path):
    _build_two_cycle_history(tmp_path)
    history = load_history(tmp_path)
    series = per_symbol_series(history)
    assert set(series) == {"AAA", "BBB"}
    assert len(series["AAA"].points) == 2
    assert len(series["BBB"].points) == 1


def test_top_k_churn_ranks_by_appearances(tmp_path: Path):
    _build_two_cycle_history(tmp_path)
    history = load_history(tmp_path)
    churn = compute_top_k_churn(history, k=5, sort_key="range_pct")
    # AAA in both cycles -> 2; BBB in one -> 1.
    by_sym = {e.symbol: e for e in churn.entries}
    assert by_sym["AAA"].appearances == 2
    assert by_sym["BBB"].appearances == 1
    assert churn.entries[0].symbol == "AAA"  # ranked first


def test_scan_code_predictiveness_groups_correctly(tmp_path: Path):
    _build_two_cycle_history(tmp_path)
    history = load_history(tmp_path)
    pred = scan_code_predictiveness(history, metric="range_pct")
    codes = [c for c, _, _ in pred.by_code]
    # HOT_BY_VOLUME appears in both AAA points; TOP_PERC_GAIN only on BBB;
    # MOST_ACTIVE only on AAA's second cycle.
    assert "HOT_BY_VOLUME" in codes
    assert "TOP_PERC_GAIN" in codes
    assert "MOST_ACTIVE" in codes
    n_by_code = {c: n for c, _, n in pred.by_code}
    assert n_by_code["HOT_BY_VOLUME"] == 2
    assert n_by_code["TOP_PERC_GAIN"] == 1
    assert n_by_code["MOST_ACTIVE"] == 1


def test_long_form_csv_writes_rows(tmp_path: Path):
    _build_two_cycle_history(tmp_path)
    history = load_history(tmp_path)
    out = tmp_path / "long.csv"
    write_long_form_csv(history, out, metrics=("range_pct", "abs_return_pct"))
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "ts,symbol,bucket,metric,value"
    # Two cycles, 3 rows total (AAA twice + BBB once) * 2 metrics = 6 rows.
    assert len(text.splitlines()) == 1 + 6


def test_load_history_empty_root(tmp_path: Path):
    assert load_history(tmp_path) == []
