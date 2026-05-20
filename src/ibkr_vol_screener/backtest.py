"""Offline backtest + weight tuning over saved snapshot history.

Walks pairs of consecutive snapshots (T_n, T_n+1). For each symbol
present in both, treats T_n's metrics as the *prediction* and T_n+1's
metrics as the *forward outcome*. Reports:

  - per-metric Spearman correlation with the forward outcome
  - top-K-by-composite-score performance vs the baseline universe
  - (optional) suggested score weights derived from correlations

Read-only by construction: no IBKR connection. All inputs come from
`snapshot.load_snapshot` via `analytics.load_history`.

Limitations:
  - Snapshot pairs assumed dense (watch mode @ 30-60 s). Sparse pairs
    yield long-horizon outcomes the metrics aren't designed for.
  - Weight tuning is naive correlation-proportional — a starting
    point, not a model. Document and require human review.
"""

from __future__ import annotations

import csv
import logging
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .analytics import load_history
from .metrics import (
    DEFAULT_SCORE_WEIGHTS,
    ScreenRow,
    compute_composite_score,
)

log = logging.getLogger(__name__)


# Metrics we evaluate / re-weight. Must match the keys understood by
# compute_composite_score so the suggested weights are usable as-is.
TUNABLE_METRICS: tuple[str, ...] = (
    "range_pct",
    "atr_pct_60m",
    "vwap_dev_abs",
    "accel_factor",
    "realized_vol_60m",
)


@dataclass(frozen=True)
class ForwardOutcome:
    symbol: str
    bucket: str
    score_at_t0: float
    metrics_at_t0: dict[str, float]
    forward_return_pct: float
    forward_range_pct: float
    t0: datetime
    t1: datetime


@dataclass(frozen=True)
class MetricCorrelation:
    metric: str
    n: int
    spearman: float

    @property
    def spearman_abs(self) -> float:
        return abs(self.spearman)


@dataclass
class TopKSummary:
    k: int
    target: str
    n_outcomes: int
    topk_mean: float
    topk_median: float
    topk_win_rate: float
    baseline_mean: float
    baseline_median: float
    baseline_win_rate: float

    @property
    def mean_lift(self) -> float:
        return self.topk_mean - self.baseline_mean


@dataclass
class BacktestReport:
    pairs: int
    outcomes: list[ForwardOutcome]
    correlations: list[MetricCorrelation] = field(default_factory=list)
    top_k_summary: TopKSummary | None = None
    target: str = "forward_return_pct"
    suggested_weights: dict[str, float] | None = None


# ----- Spearman correlation (pure stdlib) -----


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks (1-based), with ties handled scipy-style."""
    n = len(values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based
        for idx in range(i, j + 1):
            ranks[order[idx]] = avg_rank
        i = j + 1
    return ranks


def _spearman(x: Sequence[float], y: Sequence[float]) -> float:
    if len(x) != len(y) or len(x) < 2:
        return 0.0
    rx = _ranks(x)
    ry = _ranks(y)
    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den_x = math.sqrt(sum((a - mx) ** 2 for a in rx))
    den_y = math.sqrt(sum((b - my) ** 2 for b in ry))
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


# ----- outcome construction -----


def _metric_at_t0(row: ScreenRow, key: str) -> float:
    """Map a TUNABLE_METRIC key to a ScreenRow attribute. Mirrors the
    same special case as compute_composite_score for vwap_dev_abs."""
    if key == "vwap_dev_abs":
        return abs(float(row.vwap_dev_pct))
    return float(getattr(row, key, 0.0))


def build_forward_outcomes(
    snapshots_root: Path,
    *,
    score_weights: dict[str, float] | None = None,
) -> list[ForwardOutcome]:
    history = load_history(snapshots_root)
    if len(history) < 2:
        log.warning(
            "Need >= 2 snapshots for backtest; found %d under %s",
            len(history),
            snapshots_root,
        )
        return []
    weights = score_weights or DEFAULT_SCORE_WEIGHTS

    outcomes: list[ForwardOutcome] = []
    for (t0, rows_0), (t1, rows_1) in zip(history[:-1], history[1:], strict=False):
        by_sym_1 = {r.symbol: r for r in rows_1}
        for row_0 in rows_0:
            row_1 = by_sym_1.get(row_0.symbol)
            if row_1 is None:
                continue
            outcomes.append(
                ForwardOutcome(
                    symbol=row_0.symbol,
                    bucket=row_0.bucket,
                    score_at_t0=compute_composite_score(row_0, weights),
                    metrics_at_t0={
                        m: _metric_at_t0(row_0, m) for m in TUNABLE_METRICS
                    },
                    forward_return_pct=float(row_1.signed_return_pct),
                    forward_range_pct=float(row_1.range_pct),
                    t0=t0,
                    t1=t1,
                )
            )
    log.info(
        "Built %d forward outcomes from %d snapshot pairs",
        len(outcomes),
        len(history) - 1,
    )
    return outcomes


def _target_values(outcomes: list[ForwardOutcome], target: str) -> list[float]:
    if target == "forward_return_pct":
        return [o.forward_return_pct for o in outcomes]
    if target == "forward_range_pct":
        return [o.forward_range_pct for o in outcomes]
    raise ValueError(
        f"Unknown target {target!r}; choose forward_return_pct or forward_range_pct"
    )


def per_metric_correlations(
    outcomes: list[ForwardOutcome],
    *,
    target: str = "forward_return_pct",
) -> list[MetricCorrelation]:
    if not outcomes:
        return []
    target_values = _target_values(outcomes, target)
    result: list[MetricCorrelation] = []
    for metric in TUNABLE_METRICS:
        xs = [o.metrics_at_t0[metric] for o in outcomes]
        rho = _spearman(xs, target_values)
        result.append(MetricCorrelation(metric=metric, n=len(outcomes), spearman=rho))
    result.sort(key=lambda c: c.spearman_abs, reverse=True)
    return result


def top_k_performance(
    outcomes: list[ForwardOutcome],
    *,
    k: int = 10,
    target: str = "forward_return_pct",
) -> TopKSummary:
    if not outcomes:
        return TopKSummary(
            k=k, target=target, n_outcomes=0,
            topk_mean=0.0, topk_median=0.0, topk_win_rate=0.0,
            baseline_mean=0.0, baseline_median=0.0, baseline_win_rate=0.0,
        )

    sorted_outcomes = sorted(outcomes, key=lambda o: o.score_at_t0, reverse=True)
    top = sorted_outcomes[:k]
    all_vals = _target_values(outcomes, target)
    top_vals = _target_values(list(top), target)

    def _win_rate(values: list[float]) -> float:
        if not values:
            return 0.0
        return sum(1 for v in values if v > 0) / len(values)

    return TopKSummary(
        k=k,
        target=target,
        n_outcomes=len(outcomes),
        topk_mean=statistics.fmean(top_vals) if top_vals else 0.0,
        topk_median=statistics.median(top_vals) if top_vals else 0.0,
        topk_win_rate=_win_rate(top_vals),
        baseline_mean=statistics.fmean(all_vals),
        baseline_median=statistics.median(all_vals),
        baseline_win_rate=_win_rate(all_vals),
    )


def suggest_weights(correlations: list[MetricCorrelation]) -> dict[str, float] | None:
    """Normalize |Spearman| values across tunable metrics so they sum to 1.0.

    Returns None when every correlation is exactly zero (no signal in the
    history at all).
    """
    if not correlations:
        return None
    total = sum(c.spearman_abs for c in correlations)
    if total == 0:
        return None
    return {c.metric: c.spearman_abs / total for c in correlations}


def run_backtest(
    snapshots_root: Path,
    *,
    k: int = 10,
    target: str = "forward_range_pct",
    score_weights: dict[str, float] | None = None,
    tune_weights: bool = False,
) -> BacktestReport:
    outcomes = build_forward_outcomes(snapshots_root, score_weights=score_weights)
    correlations = per_metric_correlations(outcomes, target=target)
    summary = top_k_performance(outcomes, k=k, target=target) if outcomes else None
    suggested = suggest_weights(correlations) if tune_weights else None
    pairs = max(0, len({(o.t0, o.t1) for o in outcomes}))  # unique pair count
    return BacktestReport(
        pairs=pairs,
        outcomes=outcomes,
        correlations=correlations,
        top_k_summary=summary,
        target=target,
        suggested_weights=suggested,
    )


# ----- writers / formatters -----


def write_outcomes_csv(report: BacktestReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["t0", "t1", "symbol", "bucket", "score_at_t0",
             "forward_return_pct", "forward_range_pct"]
        )
        for o in report.outcomes:
            w.writerow([
                o.t0.isoformat(), o.t1.isoformat(),
                o.symbol, o.bucket,
                f"{o.score_at_t0:.6f}",
                f"{o.forward_return_pct:.6f}",
                f"{o.forward_range_pct:.6f}",
            ])
    log.info("Wrote %d outcomes -> %s", len(report.outcomes), path)


def format_weight_toml(weights: dict[str, float], n_outcomes: int) -> str:
    """Render a paste-ready TOML block with a sample-size warning."""
    lines = [
        "# Suggested by `backtest --tune-weights`.",
        f"# Derived from {n_outcomes} forward outcomes. Review before",
        "# committing; small sample sizes overfit easily.",
        "[score]",
        "weights = {",
    ]
    items = list(weights.items())
    for i, (key, value) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f"    {key} = {value:.4f}{comma}")
    lines.append("}")
    return "\n".join(lines)
