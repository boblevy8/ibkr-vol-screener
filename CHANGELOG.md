# Changelog

All notable changes to `ibkr-vol-screener` are recorded here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.5.0] — 2026-05-20

HK/Asia bucket + offline backtest + weight tuning.

### Added
- **`MarketBucket.HK`** — fifth market bucket. Opt-in via
  `--markets hk`. Live-confirmed location codes: `STK.HK.SEHK`
  (Hong Kong, default), `STK.HK.TSE_JPN` (Japan), `STK.HK.SEHKNTL`
  (Shanghai-HK Connect), `STK.HK.SEHKSTAR`, `STK.HK` (catch-all).
  Same runtime-discovery + probe + cache pattern as TSX / UK-EU.
- **`backtest <snapshots-dir>` command** — offline-only. Walks
  consecutive snapshot pairs (T_n, T_n+1), measures each symbol's
  forward outcome using T_n+1's bars, and reports:
  1. Run overview (pairs + outcomes).
  2. Per-metric Spearman correlation with the forward target.
  3. Top-K-by-composite-score performance vs the full-universe
     baseline (mean / median / win-rate lift).
- **`--tune-weights`** suggests a `[score] weights = { ... }` TOML
  block whose values are proportional to each metric's
  `|Spearman correlation|` against the chosen target. Comments warn
  about small-sample overfitting.
- Pure-stdlib Spearman implementation with average-rank tie
  handling (matches scipy's convention).

### Changed
- `pyproject.toml` bumped to `0.5.0`.

## [0.4.0] — 2026-05-19

Multi-timeframe windows + composite score.

### Added
- Per-window metric fields on `ScreenRow` for 5m / 15m / 30m
  alongside the existing 60m view: `range_pct_*`, `atr_pct_*`,
  `realized_vol_*`, `vwap_dev_pct_*`, `signed_return_pct_*`
  (15 new fields total).
- `accel_factor` = `range_pct_15m / range_pct_60m`. Lights up names
  whose recent 15 minutes did most of the hour's work. Values > 1
  indicate the last 15 minutes are outpacing the full-hour average.
- `composite_score` field — configurable weighted blend of
  `range_pct`, `atr_pct_60m`, `|vwap_dev_pct|`, `accel_factor`,
  `realized_vol_60m`, with per-component clipping so a single
  pathological value can't dominate. Default weights:
  `range_pct=0.30, atr_pct_60m=0.20, vwap_dev_abs=0.15,
  accel_factor=0.25, realized_vol_60m=0.10`.
- TOML config: new `[score] weights = { ... }` table. Unknown keys
  logged + ignored; missing keys keep defaults.
- New table columns: `Score` (P0, always shown), `Accel` (P1, width
  ≥ 100; green when > 1.0), `R15%` (P2, width ≥ 120). CSV/JSON
  exports include the full multi-timeframe field set.
- New sort keys: `composite_score`, `accel_factor`, plus every
  windowed field. All also accepted by `--filter` expressions.

### Changed
- `compute_metrics` refactored around a `_WindowMetrics` helper so
  every window shares the same metric pipeline. `slice_last_60min`
  preserved as a thin shim for back-compat.
- Default `--sort` stays `range_pct`; `--sort composite_score` is
  opt-in.
- `pyproject.toml` bumped to `0.4.0`.

### Backwards compatibility
- All existing `ScreenRow` fields preserved. New fields default to
  0.0 / `composite_score=0.0` so snapshots saved on v0.3.0 still
  load — replay recomputes the new metrics from the saved bars so
  old snapshots gain acceleration signals "for free".

## [0.3.0] — 2026-05-19

Charts, watchlist, and composable filters.

### Added
- `chart` command renders matplotlib figures from a saved snapshot
  (top-K close-price grid) or from a directory of snapshots
  (`--history` mode: multi-cycle line plot of a chosen metric).
  Save with `--out chart.png|.svg|.pdf`; optionally `--show` to open
  in a GUI viewer. Matplotlib is an optional dep —
  `pip install ibkr-vol-screener[charts]`.
- `--watchlist PATH` on `once` / `watch` reads a one-symbol-per-line
  file (comments + blanks ignored), qualifies each symbol via
  `reqContractDetailsAsync`, and adds them to the candidate pool
  alongside scanner output (deduped by conId). Config file also
  supports `watchlist = [...]` or `watchlist_file = "..."`.
- `--filter EXPR` on `once` / `watch` / `replay` accepts full expressions:
  parentheses, mixed AND/OR, comparison operators, scientific notation.
  Example: `--filter '(range_pct>3 AND volume_60m>5e5) OR atr_pct_60m>5'`.
  Multiple `--filter` flags compose with implicit AND. None-valued
  attributes (e.g. gap_pct with no --with-gap) evaluate as False.
- `WATCHLIST` is now a recognized `source_scan_codes` tag so
  watchlist-only rows still appear in scan-code-predictiveness analytics.

### Changed
- `pyproject.toml` bumped to `0.3.0`; new `[charts]` extra.

## [0.2.0] — 2026-05-19

Table UX overhaul + snapshot analytics.

### Added
- Inline `60m` sparkline column showing the shape of the last 60
  minutes of closes (unicode block characters in the live table, raw
  arrays in JSON, an inline `<svg>` polyline in HTML, and both an
  inline sparkline string + comma-separated closes in CSV).
- Responsive column dropping in `render_table`: at narrow terminal
  widths the low-priority columns (Scans, $Vol60, RVol%, VWdev%) are
  hidden so the surviving columns fit without truncation. Priority
  thresholds: P1 at width ≥ 100, P2 at ≥ 120, P3 at ≥ 140, P4 at ≥ 160.
- `--width N` flag on `once`, `watch`, `replay`, and `analyze` to force
  a specific console width (also honored via the
  `IBKR_VOL_SCREENER_WIDTH` env var).
- `--no-progress` flag on `once` to suppress the live progress widget
  during the historical-bar phase (watch already suppresses it).
- New `analyze <snapshots-dir>` command. Walks all snapshot
  subdirectories, recomputes metrics, and renders:
  1. Run overview (cycles, time span, unique symbols)
  2. Top-K churn (symbols by # appearances in the top-K of `--sort`)
  3. Persistent-symbols time series with per-symbol sparklines of the
     selected metric
  4. Scan-code predictiveness (mean of `--metric` per source scan code)
  `--csv out.csv` exports long-form `(ts, symbol, bucket, metric, value)`.
- `ScreenRow.closes_60m: tuple[float, ...]` populated by
  `compute_metrics` (sequence of 1-min closes within the window).

### Changed
- `pyproject.toml` and `__init__.__version__` bumped to `0.2.0`.

## [0.1.0] — 2026-05-19

Initial release. Read-only IBKR TWS API screener for the most volatile
stocks over the last 60 minutes.

### Added
- Four market universes: `us_major` (`STK.US.MAJOR`), `tsx`
  (`STK.NA.CANADA` discovered at runtime), `otc` (`STK.US.MINOR`),
  and an opt-in `uk_eu` (`STK.EU.LSE` default).
- CLI commands: `once`, `watch`, `scanner-params`, `config-example`.
- Metrics over the last 60 minutes of 1-minute bars:
  `range_pct`, `signed_return_pct`, `abs_return_pct`,
  `realized_vol_60m`, `atr_pct_60m`, `vwap_60m`, `vwap_dev_pct`,
  `gap_pct` (opt-in via `--with-gap`), `volume_60m`,
  `dollar_volume_60m`.
- Output writers: Rich console table, CSV, JSON, HTML.
- Configurable per-market filters (min price, min volume, min dollar
  volume, optional max price) with stricter OTC defaults.
- Sort keys: `range_pct` (default), `abs_return_pct`, `volume_60m`,
  `dollar_volume_60m`, `realized_vol_60m`, `atr_pct_60m`,
  `vwap_dev_pct`, `gap_pct`.
- Friendlier connection-error hints (refused / timeout / wrong port /
  client-id collision).
- Rotating `--log-file` (10 MB × 5 backups) at DEBUG level.
- In-memory subscription-error tracker for IBKR codes
  354 / 10089-91 / 10168 with an end-of-run summary.
- Bounded-concurrent historical fetch with exponential backoff on
  pacing errors (code 162); OTC fallback from TRADES to MIDPOINT
  when 0 bars come back.
- Filter for benign IBKR "scanner subscription cancelled" completion
  receipts so they don't appear as console errors.
- AST-level read-only invariant check in `tests/test_readonly.py`
  forbidding any order- or account-related endpoint call.

### Tooling
- `pip install -e .[dev]` or `uv sync`; Python 3.11+.
- `ruff` lint + format, `pytest` + `pytest-asyncio` test runner.
- 59 unit tests + 1 live integration test (guarded by
  `IBKR_LIVE_TEST=1`).

### Live-verified
- Paper TWS, server version 178 (2026-05-19).
- `scanner-params --refresh` parses the ~2 MB live XML.
- `once --markets us_major --with-gap --log-file` returns a
  populated ranked table with all metrics populated.
