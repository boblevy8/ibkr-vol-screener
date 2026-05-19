"""Tests for the alerts state machine and log writer (no IBKR required)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ibkr_vol_screener.alerts import (
    AlertRule,
    AlertState,
    build_rules_from_kwargs,
    write_alerts,
)
from ibkr_vol_screener.metrics import ScreenRow


def _row(
    symbol: str = "AAA",
    *,
    range_pct: float = 0.0,
    abs_return_pct: float = 0.0,
    vwap_dev_pct: float = 0.0,
    gap_pct: float | None = None,
    atr_pct_60m: float = 0.0,
    volume_60m: float = 100_000,
    bucket: str = "us_major",
) -> ScreenRow:
    t = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    return ScreenRow(
        symbol=symbol,
        con_id=hash(symbol) & 0xFFFFFFF,
        bucket=bucket,
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        first_bar_time=t,
        last_bar_time=t,
        n_bars=60,
        first_open=10.0,
        last_close=10.0,
        high_60m=10.5,
        low_60m=9.5,
        range_pct=range_pct,
        signed_return_pct=abs_return_pct,
        abs_return_pct=abs_return_pct,
        volume_60m=volume_60m,
        dollar_volume_60m=volume_60m * 10,
        realized_vol_60m=0.0,
        source_scan_codes=(),
        atr_pct_60m=atr_pct_60m,
        vwap_dev_pct=vwap_dev_pct,
        gap_pct=gap_pct,
    )


def test_alert_fires_when_threshold_crossed():
    rule = AlertRule(metric="range_pct", threshold=3.0)
    state = AlertState()
    fired = state.evaluate([_row(range_pct=5.0)], [rule])
    assert len(fired) == 1
    assert fired[0].symbol == "AAA"
    assert fired[0].value == 5.0


def test_alert_does_not_re_fire_until_drop_below_rearm():
    rule = AlertRule(metric="range_pct", threshold=3.0)
    state = AlertState(rearm_factor=0.8)
    # Cycle 1: crosses -> fires.
    fired1 = state.evaluate([_row(range_pct=5.0)], [rule])
    assert len(fired1) == 1
    # Cycle 2: still above; no re-fire.
    fired2 = state.evaluate([_row(range_pct=4.5)], [rule])
    assert fired2 == []
    # Cycle 3: drops to 2.0 -> below 0.8 * 3.0 = 2.4 -> re-armed.
    fired3 = state.evaluate([_row(range_pct=2.0)], [rule])
    assert fired3 == []
    # Cycle 4: crosses again -> fires.
    fired4 = state.evaluate([_row(range_pct=3.5)], [rule])
    assert len(fired4) == 1


def test_dead_zone_does_not_rearm():
    rule = AlertRule(metric="range_pct", threshold=10.0)
    state = AlertState(rearm_factor=0.8)
    # Fire.
    state.evaluate([_row(range_pct=12.0)], [rule])
    # Drop to 9.0 (between 8.0 rearm and 10.0 threshold) -> still armed=False.
    state.evaluate([_row(range_pct=9.0)], [rule])
    # Spike back to 11 should NOT re-fire (never crossed below 8.0).
    fired = state.evaluate([_row(range_pct=11.0)], [rule])
    assert fired == []


def test_signed_magnitude_compares_absolute_value():
    rule = AlertRule(metric="vwap_dev_pct", threshold=2.0, signed_magnitude=True)
    state = AlertState()
    fired = state.evaluate([_row(vwap_dev_pct=-3.0)], [rule])
    assert len(fired) == 1
    assert fired[0].value == -3.0


def test_missing_metric_does_not_crash_or_fire():
    rule = AlertRule(metric="gap_pct", threshold=5.0, signed_magnitude=True)
    state = AlertState()
    fired = state.evaluate([_row(gap_pct=None)], [rule])
    assert fired == []


def test_multiple_rules_fire_independently():
    r1 = AlertRule(metric="range_pct", threshold=3.0)
    r2 = AlertRule(metric="abs_return_pct", threshold=2.0)
    state = AlertState()
    fired = state.evaluate(
        [_row(range_pct=5.0, abs_return_pct=4.0)],
        [r1, r2],
    )
    assert {f.rule.metric for f in fired} == {"range_pct", "abs_return_pct"}


def test_build_rules_from_kwargs_maps_known_options():
    rules = build_rules_from_kwargs(
        alert_range_pct=2.5,
        alert_vwap_dev_pct=1.0,
        alert_gap_pct=None,  # ignored
    )
    by_metric = {r.metric: r for r in rules}
    assert "range_pct" in by_metric
    assert "vwap_dev_pct" in by_metric
    assert by_metric["vwap_dev_pct"].signed_magnitude is True
    assert by_metric["range_pct"].signed_magnitude is False


def test_write_alerts_appends_lines(tmp_path: Path):
    path = tmp_path / "alerts.log"
    rule = AlertRule(metric="range_pct", threshold=3.0)
    state = AlertState()
    fired = state.evaluate([_row(range_pct=5.0)], [rule])
    write_alerts(path, fired)
    # Re-fire after rearm.
    state.evaluate([_row(range_pct=1.0)], [rule])  # rearm
    fired2 = state.evaluate([_row(range_pct=6.0)], [rule])
    write_alerts(path, fired2)
    text = path.read_text(encoding="utf-8")
    lines = text.strip().splitlines()
    assert len(lines) == 2
    assert "range_pct>=3" in lines[0]
    assert "AAA" in lines[0]


def test_write_alerts_no_op_when_empty(tmp_path: Path):
    write_alerts(tmp_path / "x.log", [])
    assert not (tmp_path / "x.log").exists()
