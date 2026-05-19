"""Persist a full screen run to disk so it can be replayed offline.

Disk layout (one snapshot directory):

    <root>/
      snapshot.json          # meta + candidates + run config
      prior_closes.json      # optional: {conId: close} when --with-gap was used
      bars/
        <conId>.json         # list of [iso_ts, o, h, l, c, v] tuples per candidate

The `replay` CLI command loads a snapshot, recomputes metrics with any
overrides the user provides, and renders a fresh table — **without
touching IBKR**. The read-only AST check enforces that no IB client
endpoint is used from this module.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config, MarketBucket, config_summary
from .historical import BarsResult
from .scanners import Candidate

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class _TupleBar:
    """Loaded-from-snapshot bar; satisfies the BarLike Protocol."""

    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class LoadedSnapshot:
    cfg_dict: dict[str, Any]
    candidates: list[Candidate]
    bars_by_conid: dict[int, list[_TupleBar]] = field(default_factory=dict)
    prior_closes: dict[int, float] = field(default_factory=dict)
    what_to_show_by_conid: dict[int, str] = field(default_factory=dict)
    captured_at: str | None = None


def _candidate_to_dict(c: Candidate) -> dict[str, Any]:
    return {
        "con_id": c.con_id,
        "symbol": c.symbol,
        "exchange": c.exchange,
        "primary_exchange": c.primary_exchange,
        "currency": c.currency,
        "bucket": c.bucket.value if isinstance(c.bucket, MarketBucket) else str(c.bucket),
        "source_scan_codes": sorted(c.source_scan_codes),
    }


def _candidate_from_dict(d: dict[str, Any]) -> Candidate:
    return Candidate(
        con_id=int(d["con_id"]),
        symbol=str(d.get("symbol") or ""),
        exchange=str(d.get("exchange") or ""),
        primary_exchange=str(d.get("primary_exchange") or ""),
        currency=str(d.get("currency") or ""),
        bucket=MarketBucket(d["bucket"]),
        source_scan_codes=set(d.get("source_scan_codes") or ()),
    )


def _bar_to_tuple(bar: Any) -> list:
    """Convert an ib_async BarData (or our _TupleBar) to a compact list."""
    d = bar.date
    if isinstance(d, datetime):
        iso = d.astimezone(UTC).isoformat() if d.tzinfo else d.isoformat()
    else:
        # date / epoch fallback
        try:
            iso = datetime.fromtimestamp(float(d), tz=UTC).isoformat()
        except Exception:
            iso = str(d)
    return [iso, float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume)]


def _tuple_to_bar(t: list) -> _TupleBar:
    iso, o, h, low, c, v = t[0], t[1], t[2], t[3], t[4], t[5]
    if isinstance(iso, (int, float)):
        ts = datetime.fromtimestamp(float(iso), tz=UTC)
    else:
        ts = datetime.fromisoformat(str(iso))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    return _TupleBar(
        date=ts,
        open=float(o),
        high=float(h),
        low=float(low),
        close=float(c),
        volume=float(v),
    )


def save_snapshot(
    root: Path,
    cfg: Config,
    candidates: list[Candidate],
    bar_results: list[BarsResult],
    prior_closes: dict[int, float] | None = None,
) -> Path:
    """Write a snapshot to `root`. Creates the directory if needed.

    Returns the path. Safe to call repeatedly with different roots.
    """
    root = Path(root)
    bars_dir = root / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "schema": SCHEMA_VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "host": cfg.host,
        "port": cfg.port,
        "cfg": config_summary(cfg),
        "candidates": [_candidate_to_dict(c) for c in candidates],
        "what_to_show": {
            str(br.candidate.con_id): br.what_to_show for br in bar_results
        },
    }
    (root / "snapshot.json").write_text(
        json.dumps(meta, indent=2, default=str), encoding="utf-8"
    )

    if prior_closes:
        # JSON dict keys must be strings.
        (root / "prior_closes.json").write_text(
            json.dumps({str(k): v for k, v in prior_closes.items()}, indent=2),
            encoding="utf-8",
        )

    # One bars file per conId. Empty bar lists are still written so the
    # snapshot is self-describing (load_snapshot sees the row).
    for br in bar_results:
        path = bars_dir / f"{br.candidate.con_id}.json"
        path.write_text(
            json.dumps([_bar_to_tuple(b) for b in br.bars]),
            encoding="utf-8",
        )

    log.info(
        "Snapshot saved to %s (candidates=%d, bars files=%d)",
        root,
        len(candidates),
        len(bar_results),
    )
    return root


def load_snapshot(root: Path) -> LoadedSnapshot:
    """Load a snapshot directory and return parsed structures.

    Raises FileNotFoundError when the directory or `snapshot.json` is
    missing, and ValueError when the schema version is unknown.
    """
    root = Path(root)
    meta_path = root / "snapshot.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"No snapshot.json in {root}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    schema = int(meta.get("schema", 0))
    if schema != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported snapshot schema={schema}; this build expects {SCHEMA_VERSION}"
        )

    candidates = [_candidate_from_dict(d) for d in meta.get("candidates", [])]
    what_to_show_raw: dict = meta.get("what_to_show", {})
    what_to_show_by_conid = {int(k): str(v) for k, v in what_to_show_raw.items()}

    bars_by_conid: dict[int, list[_TupleBar]] = {}
    bars_dir = root / "bars"
    if bars_dir.exists():
        for fp in bars_dir.iterdir():
            if not fp.is_file() or fp.suffix != ".json":
                continue
            try:
                con_id = int(fp.stem)
            except ValueError:
                log.debug("Skipping non-conId bars file %s", fp)
                continue
            raw = json.loads(fp.read_text(encoding="utf-8"))
            bars_by_conid[con_id] = [_tuple_to_bar(t) for t in raw]

    prior_closes: dict[int, float] = {}
    pc_path = root / "prior_closes.json"
    if pc_path.exists():
        raw_pc = json.loads(pc_path.read_text(encoding="utf-8"))
        prior_closes = {int(k): float(v) for k, v in raw_pc.items()}

    return LoadedSnapshot(
        cfg_dict=meta.get("cfg") or {},
        candidates=candidates,
        bars_by_conid=bars_by_conid,
        prior_closes=prior_closes,
        what_to_show_by_conid=what_to_show_by_conid,
        captured_at=meta.get("captured_at"),
    )


@dataclass
class ReplayInputs:
    """Convenience bundle returned to the CLI replay command."""

    captured_at: str | None
    candidates: list[Candidate]
    bars_by_conid: dict[int, list[_TupleBar]]
    prior_closes: dict[int, float]
    what_to_show_by_conid: dict[int, str]
    cfg_dict: dict[str, Any]


def open_for_replay(root: Path) -> ReplayInputs:
    snap = load_snapshot(root)
    return ReplayInputs(
        captured_at=snap.captured_at,
        candidates=snap.candidates,
        bars_by_conid=snap.bars_by_conid,
        prior_closes=snap.prior_closes,
        what_to_show_by_conid=snap.what_to_show_by_conid,
        cfg_dict=snap.cfg_dict,
    )


__all__ = [
    "SCHEMA_VERSION",
    "LoadedSnapshot",
    "ReplayInputs",
    "load_snapshot",
    "open_for_replay",
    "save_snapshot",
]
