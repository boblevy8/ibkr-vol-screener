"""Threshold-based alerts for watch mode.

A run is "watching" a list of (metric, threshold) rules. Each cycle:
  1. evaluate every rule against every row,
  2. fire only the rules that newly crossed (hysteresis: re-arm only when
     the value drops back below ``rearm_factor * threshold``),
  3. append a structured line to ``alerts.log``,
  4. expose the set of alerted symbols so ``render_table`` can highlight
     those rows.

This module has zero IBKR dependencies. Tests cover the state machine
without any I/O.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .metrics import ScreenRow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertRule:
    metric: str             # ScreenRow attribute name (e.g. "range_pct")
    threshold: float
    signed_magnitude: bool = False  # True -> compare abs(value) against threshold


@dataclass(frozen=True)
class FiredAlert:
    symbol: str
    bucket: str
    rule: AlertRule
    value: float
    fired_at: datetime

    def to_log_line(self) -> str:
        op = ">=|.|"  # signed magnitude
        if not self.rule.signed_magnitude:
            op = ">="
        return (
            f"{self.fired_at.isoformat()} {self.symbol} {self.bucket} "
            f"{self.rule.metric}{op}{self.rule.threshold:g} value={self.value:.4f}"
        )


def _value_for_rule(row: ScreenRow, rule: AlertRule) -> float | None:
    """Return the numeric value to compare against the rule threshold,
    or None when the row has no value for that metric (e.g. gap_pct=None).
    """
    if not hasattr(row, rule.metric):
        return None
    v = getattr(row, rule.metric)
    if v is None:
        return None
    return float(v)


@dataclass
class AlertState:
    """Per-(symbol, rule) hysteresis tracker.

    A rule fires when:
      - the symbol has never fired this rule before, and the value is at
        or above the threshold; OR
      - the symbol was previously fired, dropped below ``rearm_factor *
        threshold``, and is now back at/above the threshold.
    """

    rearm_factor: float = 0.8
    _armed: dict[tuple[str, AlertRule], bool] = field(default_factory=dict)
    # _armed[k] = True  -> may fire (either never fired or has re-armed since)
    # _armed[k] = False -> currently in "already fired, waiting to drop" state

    def evaluate(
        self,
        rows: list[ScreenRow],
        rules: list[AlertRule],
        *,
        now: datetime | None = None,
    ) -> list[FiredAlert]:
        fired: list[FiredAlert] = []
        now = now or datetime.now(UTC)
        for row in rows:
            for rule in rules:
                v = _value_for_rule(row, rule)
                if v is None:
                    continue
                magnitude = abs(v) if rule.signed_magnitude else v
                key = (row.symbol, rule)
                armed = self._armed.get(key, True)
                if magnitude >= rule.threshold:
                    if armed:
                        fired.append(
                            FiredAlert(
                                symbol=row.symbol,
                                bucket=row.bucket,
                                rule=rule,
                                value=v,
                                fired_at=now,
                            )
                        )
                        self._armed[key] = False
                    # If not armed, keep silent — we already alerted.
                elif magnitude < rule.threshold * self.rearm_factor:
                    # Crossed back below the re-arm threshold; ready to fire again.
                    self._armed[key] = True
                # else: in the dead-zone between rearm and threshold — leave state.
        return fired


def write_alerts(path: Path, alerts: list[FiredAlert]) -> None:
    """Append one structured line per alert to `path`. Creates parent dirs."""
    if not alerts:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for a in alerts:
            fh.write(a.to_log_line() + "\n")
    log.info("Wrote %d alert(s) to %s", len(alerts), path)


def build_rules_from_kwargs(**kwargs: float | None) -> list[AlertRule]:
    """Map CLI option names to AlertRule list.

    Recognized keys (others ignored):
      alert_range_pct, alert_abs_return_pct, alert_realized_vol,
      alert_atr_pct, alert_volume_60m, alert_dollar_volume_60m,
      alert_vwap_dev_pct (signed), alert_gap_pct (signed)
    """
    sm_keys = {"alert_vwap_dev_pct", "alert_gap_pct"}
    metric_map = {
        "alert_range_pct": "range_pct",
        "alert_abs_return_pct": "abs_return_pct",
        "alert_realized_vol": "realized_vol_60m",
        "alert_atr_pct": "atr_pct_60m",
        "alert_volume_60m": "volume_60m",
        "alert_dollar_volume_60m": "dollar_volume_60m",
        "alert_vwap_dev_pct": "vwap_dev_pct",
        "alert_gap_pct": "gap_pct",
    }
    rules: list[AlertRule] = []
    for key, val in kwargs.items():
        if val is None:
            continue
        metric = metric_map.get(key)
        if metric is None:
            continue
        rules.append(
            AlertRule(
                metric=metric,
                threshold=float(val),
                signed_magnitude=(key in sm_keys),
            )
        )
    return rules
