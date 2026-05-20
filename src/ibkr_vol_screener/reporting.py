"""Filter, rank, and render `ScreenRow`s as tables / CSV / JSON / HTML."""

from __future__ import annotations

import csv
import html
import json
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

from .config import MarketBucket, MarketProfile
from .metrics import ScreenRow

log = logging.getLogger(__name__)

_VALID_SORT_KEYS = (
    "range_pct",
    "abs_return_pct",
    "volume_60m",
    "dollar_volume_60m",
    "realized_vol_60m",
    "vwap_dev_pct",
    "atr_pct_60m",
    "gap_pct",
    # Phase 6 additions:
    "composite_score",
    "accel_factor",
    "range_pct_5m",
    "range_pct_15m",
    "range_pct_30m",
    "atr_pct_5m",
    "atr_pct_15m",
    "atr_pct_30m",
    "realized_vol_5m",
    "realized_vol_15m",
    "realized_vol_30m",
    "vwap_dev_pct_5m",
    "vwap_dev_pct_15m",
    "vwap_dev_pct_30m",
)

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
_SPARK_FLAT = "─"


def sparkline(values: Sequence[float], *, width: int = 10) -> str:
    """Compact unicode sparkline from `values`, bucketed into `width` slots.

    Returns "─" * width for flat input. Returns "" for empty input. The
    bucket aggregator averages the values in each slot so a long series
    compresses sensibly. Output is exactly `width` characters wide.
    """
    if not values or width <= 0:
        return ""
    n = len(values)
    lo = min(values)
    hi = max(values)
    rng = hi - lo
    if rng == 0:
        return _SPARK_FLAT * width
    n_blocks = len(_SPARK_BLOCKS)
    out = []
    for i in range(width):
        start = (i * n) // width
        end = ((i + 1) * n) // width
        if end <= start:
            end = start + 1
        end = min(end, n)
        avg = sum(values[start:end]) / (end - start)
        normalized = (avg - lo) / rng
        idx = int(normalized * (n_blocks - 1))
        idx = max(0, min(n_blocks - 1, idx))
        out.append(_SPARK_BLOCKS[idx])
    return "".join(out)


def _sort_value(row: ScreenRow, key: str) -> float:
    """Robust sort key extractor: None gap_pct sorts last."""
    v = getattr(row, key)
    if v is None:
        return float("-inf")
    return float(v)


def filter_rows(
    rows: Iterable[ScreenRow],
    profiles: dict[MarketBucket, MarketProfile],
) -> list[ScreenRow]:
    """Apply per-bucket min_price/volume/dollar_volume thresholds."""
    out: list[ScreenRow] = []
    dropped = 0
    for row in rows:
        try:
            bucket = MarketBucket(row.bucket)
        except ValueError:
            log.debug("Unknown bucket %r — keeping row", row.bucket)
            out.append(row)
            continue
        prof = profiles.get(bucket)
        if prof is None:
            out.append(row)
            continue
        if row.last_close < prof.min_price:
            dropped += 1
            continue
        if prof.max_price is not None and row.last_close > prof.max_price:
            dropped += 1
            continue
        if row.volume_60m < prof.min_volume_60m:
            dropped += 1
            continue
        if row.dollar_volume_60m < prof.min_dollar_volume_60m:
            dropped += 1
            continue
        out.append(row)
    if dropped:
        log.info("Filtered out %d rows below per-bucket thresholds.", dropped)
    return out


def rank_rows(
    rows: Iterable[ScreenRow], *, sort_key: str = "range_pct", top_n: int = 30
) -> list[ScreenRow]:
    if sort_key not in _VALID_SORT_KEYS:
        raise ValueError(
            f"Invalid sort_key={sort_key!r}. Choose one of: {', '.join(_VALID_SORT_KEYS)}"
        )
    # For vwap_dev_pct / gap_pct, sort by absolute magnitude so big moves in
    # either direction surface to the top. range_pct and atr_pct are already
    # non-negative; signed sort makes sense for abs_return_pct (also non-neg)
    # and the volume keys.
    signed_keys = {"vwap_dev_pct", "gap_pct"}
    if sort_key in signed_keys:
        return sorted(rows, key=lambda r: abs(_sort_value(r, sort_key)), reverse=True)[:top_n]
    return sorted(rows, key=lambda r: _sort_value(r, sort_key), reverse=True)[:top_n]


# Column priority tiers for responsive rendering. Lower tier = more important.
# Each entry: (name, header_label, min_width_to_include).
# P0 columns always render; widths below 80 still show P0 but may overflow.
# Score is P0 because it's the canonical "is this interesting?" signal.
_COLUMNS_P0 = (
    "idx", "sym", "bkt", "px", "range_pct", "signed_pct", "spark", "score", "bars"
)
_COLUMNS_P1 = ("abs_pct", "atr_pct", "accel")
_COLUMNS_P2 = ("rvol", "vwap_dev", "r15")
_COLUMNS_P3 = ("gap_pct", "vol60", "dvol60")
_COLUMNS_P4 = ("scans",)

_BUCKET_ABBREV = {"us_major": "US", "tsx": "CA", "otc": "OTC", "uk_eu": "EU"}


def _select_columns(console_width: int, show_gap: bool) -> tuple[str, ...]:
    """Greedy column selection by priority tier given a console width."""
    keep = list(_COLUMNS_P0)
    if console_width >= 100:
        keep.extend(_COLUMNS_P1)
    if console_width >= 120:
        keep.extend(_COLUMNS_P2)
    if console_width >= 140:
        for c in _COLUMNS_P3:
            if c == "gap_pct" and not show_gap:
                continue
            keep.append(c)
    if console_width >= 160:
        keep.extend(_COLUMNS_P4)
    return tuple(keep)


def render_table(
    rows: list[ScreenRow],
    *,
    sort_key: str = "range_pct",
    alerted_symbols: set[str] | None = None,
    console_width: int | None = None,
):
    """Build a Rich table for the given rows. Returns a `rich.table.Table`.

    `alerted_symbols` — rows whose symbol is in this set render bold red.
    `console_width` — if supplied, used to drop low-priority columns when
    the terminal isn't wide enough to fit them all.
    """
    from rich.table import Table

    alerted = alerted_symbols or set()
    show_gap = any(r.gap_pct is not None for r in rows)
    width = console_width if console_width is not None else 200
    selected = _select_columns(width, show_gap)

    t = Table(
        title=f"IBKR vol screener - last 60m (sorted by {sort_key})",
        show_lines=False,
        expand=False,
        pad_edge=False,
    )

    column_specs: dict[str, tuple[str, dict]] = {
        "idx": ("#", {"justify": "right", "style": "dim", "no_wrap": True}),
        "sym": ("Sym", {"style": "bold cyan", "no_wrap": True}),
        "bkt": ("Bkt", {"style": "magenta", "no_wrap": True}),
        "px": ("Px", {"justify": "right", "no_wrap": True}),
        "range_pct": (
            "Range%",
            {"justify": "right", "style": "bold yellow", "no_wrap": True},
        ),
        "signed_pct": ("Sgn%", {"justify": "right", "no_wrap": True}),
        "spark": ("60m", {"justify": "left", "no_wrap": True}),
        "score": (
            "Score",
            {"justify": "right", "style": "bold blue", "no_wrap": True},
        ),
        "bars": ("Bars", {"justify": "right", "no_wrap": True}),
        "abs_pct": ("|d|%", {"justify": "right", "no_wrap": True}),
        "atr_pct": ("ATR%", {"justify": "right", "no_wrap": True}),
        "accel": ("Accel", {"justify": "right", "no_wrap": True}),
        "rvol": ("RVol%", {"justify": "right", "no_wrap": True}),
        "vwap_dev": ("VWdev%", {"justify": "right", "no_wrap": True}),
        "r15": ("R15%", {"justify": "right", "no_wrap": True}),
        "gap_pct": ("Gap%", {"justify": "right", "no_wrap": True}),
        "vol60": ("Vol60", {"justify": "right", "no_wrap": True}),
        "dvol60": ("$Vol60", {"justify": "right", "no_wrap": True}),
        "scans": ("Scans", {"overflow": "fold"}),
    }
    for col in selected:
        header, kwargs = column_specs[col]
        t.add_column(header, **kwargs)

    for i, r in enumerate(rows, start=1):
        signed_style = "green" if r.signed_return_pct >= 0 else "red"
        vw_style = "green" if r.vwap_dev_pct >= 0 else "red"
        gap_text: str
        if r.gap_pct is None:
            gap_text = "-"
        else:
            gap_style = "green" if r.gap_pct >= 0 else "red"
            gap_text = f"[{gap_style}]{r.gap_pct:+.2f}[/{gap_style}]"

        accel_style = "green" if r.accel_factor > 1.0 else "dim"
        cell_by_col = {
            "idx": str(i),
            "sym": r.symbol,
            "bkt": _BUCKET_ABBREV.get(r.bucket, r.bucket),
            "px": f"{r.last_close:.2f}",
            "range_pct": f"{r.range_pct:.2f}",
            "signed_pct": f"[{signed_style}]{r.signed_return_pct:+.2f}[/{signed_style}]",
            "spark": sparkline(r.closes_60m, width=10),
            "score": f"{r.composite_score:.2f}",
            "bars": str(r.n_bars),
            "abs_pct": f"{r.abs_return_pct:.2f}",
            "atr_pct": f"{r.atr_pct_60m:.2f}",
            "accel": f"[{accel_style}]{r.accel_factor:.2f}x[/{accel_style}]",
            "rvol": f"{r.realized_vol_60m:.0f}",
            "vwap_dev": f"[{vw_style}]{r.vwap_dev_pct:+.2f}[/{vw_style}]",
            "r15": f"{r.range_pct_15m:.2f}",
            "gap_pct": gap_text,
            "vol60": f"{r.volume_60m:,.0f}",
            "dvol60": f"{r.dollar_volume_60m:,.0f}",
            "scans": ",".join(r.source_scan_codes),
        }
        style = "bold red" if r.symbol in alerted else None
        t.add_row(*(cell_by_col[c] for c in selected), style=style)
    return t


def write_csv(rows: list[ScreenRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        # Same field set as ScreenRow dataclass + two derived fields.
        fieldnames = list(ScreenRow.__dataclass_fields__.keys()) + ["sparkline_60m"]
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            d = r.as_dict()
            d["source_scan_codes"] = ",".join(d["source_scan_codes"])
            # closes_60m: comma-separated floats; spreadsheets can split + chart.
            d["closes_60m"] = ",".join(f"{c:.4f}" for c in r.closes_60m)
            d["sparkline_60m"] = sparkline(r.closes_60m, width=10)
            writer.writerow(d)
    log.info("Wrote %d rows -> %s", len(rows), path)


def write_json(rows: list[ScreenRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for r in rows:
        d = r.as_dict()
        d["closes_60m"] = list(r.closes_60m)
        d["sparkline_60m"] = sparkline(r.closes_60m, width=10)
        payload.append(d)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log.info("Wrote %d rows -> %s", len(rows), path)


def _svg_spark(values: Sequence[float], *, width: int = 100, height: int = 20) -> str:
    """Tiny inline SVG polyline; pure str output (no html escaping needed
    since we control the construction). Returns empty string on no data."""
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    rng = hi - lo or 1.0
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = (i / (n - 1)) * width if n > 1 else width / 2
        y = height - ((v - lo) / rng) * height
        pts.append(f"{x:.2f},{y:.2f}")
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg"><polyline points="{" ".join(pts)}" '
        f'fill="none" stroke="#1a73e8" stroke-width="1.5"/></svg>'
    )


def write_html(rows: list[ScreenRow], path: Path, *, sort_key: str = "range_pct") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    show_gap = any(r.gap_pct is not None for r in rows)
    headers = [
        "#",
        "Symbol",
        "Bucket",
        "Last",
        "Range%",
        "Signed%",
        "60m",
        "|d|%",
        "RVol%",
        "ATR%",
        "VWAP_dev%",
    ]
    if show_gap:
        headers.append("Gap%")
    headers += ["Vol60", "$Vol60", "Bars", "Scans"]
    body_rows = []
    for i, r in enumerate(rows, start=1):
        spark_cell = _svg_spark(r.closes_60m) or html.escape(
            sparkline(r.closes_60m, width=10)
        )
        cells_escaped = [
            html.escape(str(i)),
            html.escape(r.symbol),
            html.escape(r.bucket),
            html.escape(f"{r.last_close:.2f}"),
            html.escape(f"{r.range_pct:.2f}"),
            html.escape(f"{r.signed_return_pct:+.2f}"),
            spark_cell,  # already safe (we built the SVG ourselves)
            html.escape(f"{r.abs_return_pct:.2f}"),
            html.escape(f"{r.realized_vol_60m:.1f}"),
            html.escape(f"{r.atr_pct_60m:.2f}"),
            html.escape(f"{r.vwap_dev_pct:+.2f}"),
        ]
        if show_gap:
            cells_escaped.append(
                html.escape("-" if r.gap_pct is None else f"{r.gap_pct:+.2f}")
            )
        cells_escaped += [
            html.escape(f"{r.volume_60m:,.0f}"),
            html.escape(f"{r.dollar_volume_60m:,.0f}"),
            html.escape(str(r.n_bars)),
            html.escape(",".join(r.source_scan_codes)),
        ]
        body_rows.append(
            "<tr>" + "".join(f"<td>{c}</td>" for c in cells_escaped) + "</tr>"
        )
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    style = (
        "<style>body{font-family:system-ui;margin:1rem;}"
        "table{border-collapse:collapse;}"
        "th,td{border:1px solid #ccc;padding:4px 8px;font-size:13px;}"
        "th{background:#f4f4f4;text-align:right;}"
        "td{text-align:right;}td:nth-child(2),td:nth-child(3),td:last-child{text-align:left;}"
        "td svg{display:block;}"
        "</style>"
    )
    doc = (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>IBKR vol screener</title>{style}</head>"
        f"<body><h1>IBKR vol screener — last 60m (sorted by {html.escape(sort_key)})</h1>"
        f"<table><thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table></body></html>"
    )
    path.write_text(doc, encoding="utf-8")
    log.info("Wrote %d rows -> %s", len(rows), path)
