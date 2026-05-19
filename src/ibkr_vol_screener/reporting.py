"""Filter, rank, and render `ScreenRow`s as tables / CSV / JSON / HTML."""

from __future__ import annotations

import csv
import html
import json
import logging
from collections.abc import Iterable
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
)


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


def render_table(rows: list[ScreenRow], *, sort_key: str = "range_pct"):
    """Build a Rich table for the given rows. Returns a `rich.table.Table`."""
    from rich.table import Table

    # Bucket abbreviations to keep columns narrow on small terminals.
    _bucket_abbrev = {"us_major": "US", "tsx": "CA", "otc": "OTC", "uk_eu": "EU"}

    show_gap = any(r.gap_pct is not None for r in rows)

    t = Table(
        title=f"IBKR vol screener - last 60m (sorted by {sort_key})",
        show_lines=False,
        expand=False,
        pad_edge=False,
    )
    t.add_column("#", justify="right", style="dim", no_wrap=True)
    t.add_column("Sym", style="bold cyan", no_wrap=True)
    t.add_column("Bkt", style="magenta", no_wrap=True)
    t.add_column("Px", justify="right", no_wrap=True)
    t.add_column("Range%", justify="right", style="bold yellow", no_wrap=True)
    t.add_column("Sgn%", justify="right", no_wrap=True)
    t.add_column("|d|%", justify="right", no_wrap=True)
    t.add_column("RVol%", justify="right", no_wrap=True)
    t.add_column("ATR%", justify="right", no_wrap=True)
    t.add_column("VWdev%", justify="right", no_wrap=True)
    if show_gap:
        t.add_column("Gap%", justify="right", no_wrap=True)
    t.add_column("Vol60", justify="right", no_wrap=True)
    t.add_column("$Vol60", justify="right", no_wrap=True)
    t.add_column("Bars", justify="right", no_wrap=True)
    t.add_column("Scans", overflow="fold")

    for i, r in enumerate(rows, start=1):
        signed = f"{r.signed_return_pct:+.2f}"
        signed_style = "green" if r.signed_return_pct >= 0 else "red"
        vw = f"{r.vwap_dev_pct:+.2f}"
        vw_style = "green" if r.vwap_dev_pct >= 0 else "red"
        row_cells = [
            str(i),
            r.symbol,
            _bucket_abbrev.get(r.bucket, r.bucket),
            f"{r.last_close:.2f}",
            f"{r.range_pct:.2f}",
            f"[{signed_style}]{signed}[/{signed_style}]",
            f"{r.abs_return_pct:.2f}",
            f"{r.realized_vol_60m:.0f}",
            f"{r.atr_pct_60m:.2f}",
            f"[{vw_style}]{vw}[/{vw_style}]",
        ]
        if show_gap:
            if r.gap_pct is None:
                row_cells.append("-")
            else:
                gap_style = "green" if r.gap_pct >= 0 else "red"
                row_cells.append(f"[{gap_style}]{r.gap_pct:+.2f}[/{gap_style}]")
        row_cells.extend(
            [
                f"{r.volume_60m:,.0f}",
                f"{r.dollar_volume_60m:,.0f}",
                str(r.n_bars),
                ",".join(r.source_scan_codes),
            ]
        )
        t.add_row(*row_cells)
    return t


def write_csv(rows: list[ScreenRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        fieldnames = list(ScreenRow.__dataclass_fields__.keys())
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            d = r.as_dict()
            # csv can't accept lists; flatten.
            d["source_scan_codes"] = ",".join(d["source_scan_codes"])
            writer.writerow(d)
    log.info("Wrote %d rows -> %s", len(rows), path)


def write_json(rows: list[ScreenRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([r.as_dict() for r in rows], indent=2, default=str),
        encoding="utf-8",
    )
    log.info("Wrote %d rows -> %s", len(rows), path)


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
        cells = [
            i,
            r.symbol,
            r.bucket,
            f"{r.last_close:.2f}",
            f"{r.range_pct:.2f}",
            f"{r.signed_return_pct:+.2f}",
            f"{r.abs_return_pct:.2f}",
            f"{r.realized_vol_60m:.1f}",
            f"{r.atr_pct_60m:.2f}",
            f"{r.vwap_dev_pct:+.2f}",
        ]
        if show_gap:
            cells.append("-" if r.gap_pct is None else f"{r.gap_pct:+.2f}")
        cells += [
            f"{r.volume_60m:,.0f}",
            f"{r.dollar_volume_60m:,.0f}",
            r.n_bars,
            ",".join(r.source_scan_codes),
        ]
        body_rows.append(
            "<tr>" + "".join(f"<td>{html.escape(str(x))}</td>" for x in cells) + "</tr>"
        )
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    style = (
        "<style>body{font-family:system-ui;margin:1rem;}"
        "table{border-collapse:collapse;}"
        "th,td{border:1px solid #ccc;padding:4px 8px;font-size:13px;}"
        "th{background:#f4f4f4;text-align:right;}"
        "td{text-align:right;}td:nth-child(2),td:nth-child(3),td:last-child{text-align:left;}"
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
