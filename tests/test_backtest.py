"""Tests for backtest.py: Spearman, forward outcomes, correlations,
top-K, weight tuning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ibkr_vol_screener.backtest import (
    TUNABLE_METRICS,
    _ranks,
    _spearman,
    build_forward_outcomes,
    per_metric_correlations,
    run_backtest,
    suggest_weights,
    top_k_performance,
)
from ibkr_vol_screener.config import Config, MarketBucket
from ibkr_vol_screener.historical import BarsResult
from ibkr_vol_screener.scanners import Candidate
from ibkr_vol_screener.snapshot import save_snapshot

from .conftest import make_bars

# -------- Spearman edge cases --------


def test_spearman_perfect_positive():
    # Within floating-point tolerance — perfect monotonic relationship.
    assert abs(_spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) - 1.0) < 1e-9


def test_spearman_perfect_negative():
    assert abs(_spearman([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) - (-1.0)) < 1e-9


def test_spearman_constant_returns_zero():
    assert _spearman([1, 2, 3], [5, 5, 5]) == 0.0
    assert _spearman([7, 7, 7], [1, 2, 3]) == 0.0


def test_spearman_too_short_returns_zero():
    assert _spearman([1.0], [2.0]) == 0.0
    assert _spearman([], []) == 0.0


def test_spearman_mismatched_length_returns_zero():
    assert _spearman([1, 2, 3], [1, 2]) == 0.0


def test_ranks_handles_ties_average():
    # Two pairs of ties: ranks should average.
    out = _ranks([10, 20, 20, 30])
    # Sorted order: 10(rank1), 20(rank2.5), 20(rank2.5), 30(rank4)
    assert out == [1.0, 2.5, 2.5, 4.0]


def test_spearman_monotonic_with_ties():
    rho = _spearman([1, 2, 2, 3], [10, 20, 20, 30])
    assert rho > 0.9


# -------- forward outcomes round-trip via snapshots --------


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


def _build_two_snapshots(tmp_path: Path, *, step_2: float = 0.2) -> None:
    """Two consecutive snapshots; AAA stays put, BBB jumps in cycle 2."""
    now = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    cands = [_cand(1, "AAA"), _cand(2, "BBB")]

    # Cycle 1
    bars_a1 = make_bars(now - timedelta(minutes=60), count=60, open_=100.0, step_pct=0.05)
    bars_b1 = make_bars(now - timedelta(minutes=60), count=60, open_=50.0, step_pct=0.05)
    save_snapshot(
        tmp_path / "2026-05-19T16-00-00",
        Config(),
        cands,
        [BarsResult(cands[0], bars=bars_a1), BarsResult(cands[1], bars=bars_b1)],
    )
    # Cycle 2 (1 minute later) — BBB has a big move forward.
    bars_a2 = make_bars(now - timedelta(minutes=59), count=60, open_=100.0, step_pct=0.05)
    bars_b2 = make_bars(now - timedelta(minutes=59), count=60, open_=50.0, step_pct=step_2)
    save_snapshot(
        tmp_path / "2026-05-19T16-01-00",
        Config(),
        cands,
        [BarsResult(cands[0], bars=bars_a2), BarsResult(cands[1], bars=bars_b2)],
    )


def test_build_forward_outcomes_pairs_symbols(tmp_path: Path):
    _build_two_snapshots(tmp_path)
    outcomes = build_forward_outcomes(tmp_path)
    by_sym = {o.symbol: o for o in outcomes}
    assert set(by_sym) == {"AAA", "BBB"}
    # BBB jumped much more than AAA in cycle 2:
    assert by_sym["BBB"].forward_return_pct > by_sym["AAA"].forward_return_pct


def test_build_forward_outcomes_returns_empty_for_single_snapshot(tmp_path: Path):
    now = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    cands = [_cand(1, "ZZZ")]
    bars = make_bars(now - timedelta(minutes=60), count=60)
    save_snapshot(tmp_path / "only", Config(), cands, [BarsResult(cands[0], bars=bars)])
    assert build_forward_outcomes(tmp_path) == []


def test_per_metric_correlations_returns_all_tunables(tmp_path: Path):
    _build_two_snapshots(tmp_path)
    outcomes = build_forward_outcomes(tmp_path)
    corrs = per_metric_correlations(outcomes, target="forward_return_pct")
    metrics = {c.metric for c in corrs}
    assert metrics == set(TUNABLE_METRICS)
    # Sorted by |spearman| desc.
    abs_values = [c.spearman_abs for c in corrs]
    assert abs_values == sorted(abs_values, reverse=True)


def test_top_k_performance_picks_high_score_outcomes():
    """Hand-construct outcomes; top-K should be the highest score subset."""
    from ibkr_vol_screener.backtest import ForwardOutcome

    t = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    outcomes = [
        ForwardOutcome(
            symbol=f"S{i}", bucket="us_major",
            score_at_t0=float(i),
            metrics_at_t0={m: 0.0 for m in TUNABLE_METRICS},
            forward_return_pct=float(i),  # higher-score names have higher fwd return
            forward_range_pct=float(i),
            t0=t, t1=t + timedelta(minutes=1),
        )
        for i in range(10)
    ]
    summary = top_k_performance(outcomes, k=3, target="forward_return_pct")
    # Top-3 by score = indices 9, 8, 7 -> mean = 8.0
    assert summary.topk_mean == 8.0
    # Baseline mean = 0+1+...+9 / 10 = 4.5
    assert summary.baseline_mean == 4.5
    assert summary.mean_lift == 3.5


def test_suggest_weights_normalizes_to_one(tmp_path: Path):
    _build_two_snapshots(tmp_path)
    outcomes = build_forward_outcomes(tmp_path)
    corrs = per_metric_correlations(outcomes, target="forward_range_pct")
    suggested = suggest_weights(corrs)
    # Either all-zero (no signal) or normalized.
    if suggested is not None:
        assert abs(sum(suggested.values()) - 1.0) < 1e-9
        assert set(suggested) == set(TUNABLE_METRICS)


def test_suggest_weights_zero_signal_returns_none():
    from ibkr_vol_screener.backtest import MetricCorrelation

    corrs = [MetricCorrelation(metric=m, n=0, spearman=0.0) for m in TUNABLE_METRICS]
    assert suggest_weights(corrs) is None


def test_run_backtest_end_to_end(tmp_path: Path):
    _build_two_snapshots(tmp_path)
    report = run_backtest(
        tmp_path,
        k=2,
        target="forward_return_pct",
        tune_weights=True,
    )
    assert report.pairs >= 1
    assert len(report.outcomes) >= 2
    assert report.correlations
    assert report.top_k_summary is not None
    assert report.suggested_weights is None or abs(
        sum(report.suggested_weights.values()) - 1.0
    ) < 1e-9


def test_run_backtest_no_data_returns_empty_report(tmp_path: Path):
    report = run_backtest(tmp_path)
    assert report.outcomes == []
    assert report.correlations == []
    assert report.top_k_summary is None
