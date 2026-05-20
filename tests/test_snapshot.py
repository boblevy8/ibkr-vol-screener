"""Snapshot save/load round-trip tests (no IBKR required)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ibkr_vol_screener.config import Config, MarketBucket
from ibkr_vol_screener.historical import BarsResult
from ibkr_vol_screener.metrics import compute_metrics
from ibkr_vol_screener.scanners import Candidate
from ibkr_vol_screener.snapshot import (
    SCHEMA_VERSION,
    load_snapshot,
    open_for_replay,
    save_snapshot,
)

from .conftest import make_bars


def _cand(con_id: int, sym: str, bucket: MarketBucket = MarketBucket.US_MAJOR) -> Candidate:
    return Candidate(
        con_id=con_id,
        symbol=sym,
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        bucket=bucket,
        source_scan_codes={"HOT_BY_VOLUME"},
    )


def test_save_load_round_trip(tmp_path: Path):
    now = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    cands = [_cand(1, "AAA"), _cand(2, "BBB")]
    bars_a = make_bars(now - timedelta(minutes=60), count=60, open_=100.0, volume=1000)
    bars_b = make_bars(now - timedelta(minutes=60), count=60, open_=50.0, volume=2000)
    bar_results = [
        BarsResult(candidate=cands[0], bars=bars_a, what_to_show="TRADES"),
        BarsResult(candidate=cands[1], bars=bars_b, what_to_show="MIDPOINT"),
    ]

    cfg = Config()
    save_snapshot(tmp_path / "snap", cfg, cands, bar_results, prior_closes={1: 99.5})

    loaded = load_snapshot(tmp_path / "snap")
    assert {c.con_id for c in loaded.candidates} == {1, 2}
    assert len(loaded.bars_by_conid[1]) == 60
    assert len(loaded.bars_by_conid[2]) == 60
    # OHLCV survives.
    b0 = loaded.bars_by_conid[1][0]
    assert b0.open == bars_a[0].open
    assert b0.close == bars_a[0].close
    assert b0.volume == bars_a[0].volume
    # Captured per-candidate what_to_show.
    assert loaded.what_to_show_by_conid[2] == "MIDPOINT"
    # Prior closes round-trip with int keys.
    assert loaded.prior_closes == {1: 99.5}


def test_open_for_replay_matches_live_metrics(tmp_path: Path):
    now = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    cand = _cand(42, "TEST")
    bars = make_bars(now - timedelta(minutes=60), count=60, open_=100.0, step_pct=0.1)
    bar_results = [BarsResult(candidate=cand, bars=bars, what_to_show="TRADES")]

    save_snapshot(tmp_path / "snap", Config(), [cand], bar_results, None)
    live_row = compute_metrics(cand, bars, min_bars=10, now=now)

    inputs = open_for_replay(tmp_path / "snap")
    replay_bars = inputs.bars_by_conid[42]
    replay_row = compute_metrics(cand, replay_bars, min_bars=10, now=now)

    assert live_row is not None and replay_row is not None
    assert abs(live_row.range_pct - replay_row.range_pct) < 1e-9
    assert abs(live_row.signed_return_pct - replay_row.signed_return_pct) < 1e-9
    assert abs(live_row.realized_vol_60m - replay_row.realized_vol_60m) < 1e-9
    assert abs(live_row.atr_pct_60m - replay_row.atr_pct_60m) < 1e-9
    assert abs(live_row.vwap_dev_pct - replay_row.vwap_dev_pct) < 1e-9


def test_load_raises_on_missing_directory(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_snapshot(tmp_path / "does_not_exist")


def test_load_raises_on_schema_mismatch(tmp_path: Path):
    root = tmp_path / "bad"
    root.mkdir()
    (root / "snapshot.json").write_text('{"schema": 999, "candidates": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        load_snapshot(root)


def test_schema_constant_is_one():
    assert SCHEMA_VERSION == 1


def test_replay_v030_snapshot_still_works(tmp_path: Path):
    """Phase 6 added 15m/30m/5m fields. Old snapshots (saved before they
    existed) should still load, because the values come from bars/* JSON
    and metrics are recomputed from those bars at replay time. The fields
    populate naturally from the recomputed metrics."""
    now = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    cand = _cand(99, "OLD")
    bars = make_bars(now - timedelta(minutes=60), count=60, open_=100.0, step_pct=0.05)
    save_snapshot(tmp_path / "v030", Config(), [cand], [BarsResult(cand, bars=bars)], None)

    loaded = load_snapshot(tmp_path / "v030")
    # Recompute via the new compute_metrics; sub-windows should populate.
    row = compute_metrics(
        loaded.candidates[0],
        loaded.bars_by_conid[99],
        min_bars=10,
        min_bars_short=3,
        now=now,
    )
    assert row is not None
    assert row.range_pct > 0
    assert row.range_pct_15m > 0  # Phase 6 field, populated from old bars
    assert row.composite_score > 0


def test_save_with_no_prior_closes_omits_file(tmp_path: Path):
    now = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    cand = _cand(7, "ZZZ")
    bars = make_bars(now - timedelta(minutes=30), count=30)
    bar_results = [BarsResult(candidate=cand, bars=bars)]
    save_snapshot(tmp_path / "snap", Config(), [cand], bar_results, None)
    assert not (tmp_path / "snap" / "prior_closes.json").exists()
    loaded = load_snapshot(tmp_path / "snap")
    assert loaded.prior_closes == {}
