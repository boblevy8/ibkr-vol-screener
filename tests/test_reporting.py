"""Tests for responsive table rendering and the updated writers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ibkr_vol_screener.metrics import ScreenRow
from ibkr_vol_screener.reporting import (
    _select_columns,
    render_table,
    write_csv,
    write_html,
    write_json,
)


def _row(symbol: str = "AAA", *, gap: float | None = None) -> ScreenRow:
    t = datetime(2026, 5, 19, 16, 0, tzinfo=UTC)
    return ScreenRow(
        symbol=symbol,
        con_id=42,
        bucket="us_major",
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        first_bar_time=t,
        last_bar_time=t,
        n_bars=60,
        first_open=10.0,
        last_close=11.0,
        high_60m=11.5,
        low_60m=9.5,
        range_pct=20.0,
        signed_return_pct=10.0,
        abs_return_pct=10.0,
        volume_60m=500_000,
        dollar_volume_60m=5_500_000,
        realized_vol_60m=50.0,
        source_scan_codes=("HOT_BY_VOLUME",),
        atr_pct_60m=2.5,
        vwap_dev_pct=1.0,
        gap_pct=gap,
        closes_60m=tuple(10.0 + i * 0.02 for i in range(60)),
    )


def test_select_columns_drops_low_priority_at_narrow_widths():
    cols_80 = _select_columns(80, show_gap=False)
    cols_100 = _select_columns(100, show_gap=False)
    cols_120 = _select_columns(120, show_gap=False)
    cols_140 = _select_columns(140, show_gap=False)
    cols_160 = _select_columns(160, show_gap=False)

    # P0 always present
    for cols in (cols_80, cols_100, cols_120, cols_140, cols_160):
        assert "sym" in cols and "range_pct" in cols and "spark" in cols and "bars" in cols

    assert "atr_pct" not in cols_80
    assert "atr_pct" in cols_100

    assert "vwap_dev" not in cols_100
    assert "vwap_dev" in cols_120

    assert "vol60" not in cols_120
    assert "vol60" in cols_140

    assert "scans" not in cols_140
    assert "scans" in cols_160


def test_select_columns_drops_gap_when_show_gap_is_false():
    cols = _select_columns(200, show_gap=False)
    assert "gap_pct" not in cols
    cols2 = _select_columns(200, show_gap=True)
    assert "gap_pct" in cols2


def test_render_table_narrow_excludes_scans():
    rows = [_row("AAA")]
    table = render_table(rows, console_width=80)
    headers = [c.header for c in table.columns]
    assert "Scans" not in headers
    assert "Range%" in headers
    assert "60m" in headers


def test_render_table_wide_includes_all():
    rows = [_row("AAA", gap=2.0)]
    table = render_table(rows, console_width=200)
    headers = [c.header for c in table.columns]
    assert "Scans" in headers
    assert "Gap%" in headers


def test_render_table_alerted_symbol_gets_style():
    rows = [_row("AAA"), _row("BBB")]
    table = render_table(rows, alerted_symbols={"BBB"}, console_width=200)
    # Find which row has style != None
    styled = [row.style for row in table.rows]
    # First row (AAA) -> None; second (BBB) -> "bold red"
    assert styled[0] is None
    assert styled[1] == "bold red"


def test_write_csv_includes_sparkline_and_closes(tmp_path: Path):
    path = tmp_path / "out.csv"
    write_csv([_row("AAA")], path)
    text = path.read_text(encoding="utf-8")
    header = text.splitlines()[0]
    assert "sparkline_60m" in header
    assert "closes_60m" in header
    # Body row should contain unicode block chars
    body = text.splitlines()[1]
    assert any(ch in body for ch in "▁▂▃▄▅▆▇█─")


def test_write_json_includes_closes_array(tmp_path: Path):
    path = tmp_path / "out.json"
    write_json([_row("AAA")], path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "closes_60m" in data[0]
    assert isinstance(data[0]["closes_60m"], list)
    assert len(data[0]["closes_60m"]) == 60
    assert "sparkline_60m" in data[0]


def test_write_html_has_inline_svg(tmp_path: Path):
    path = tmp_path / "out.html"
    write_html([_row("AAA")], path)
    text = path.read_text(encoding="utf-8")
    assert "<svg" in text
    assert "polyline" in text
    assert "60m" in text  # column header
