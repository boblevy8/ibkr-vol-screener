# Changelog

All notable changes to `ibkr-vol-screener` are recorded here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
