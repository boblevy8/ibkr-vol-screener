# ibkr-vol-screener

A **read-only** Python CLI that uses the Interactive Brokers TWS API to surface
the most volatile stocks over the **last 60 minutes** across four universes:

1. **US major** — listed on NYSE / NASDAQ / AMEX / ARCA (`STK.US.MAJOR`)
2. **Canada / TSX** — `STK.NA.CANADA` (auto-discovered; falls back to `STK.NA.TSE` / `STK.NA.VENTURE`)
3. **US OTC / Pink Sheets** (`STK.US.MINOR`)
4. **UK / EU** (opt-in via `--markets uk_eu`) — `STK.EU` catch-all by default

The tool combines IBKR market scanners (used as a candidate generator) with
freshly fetched 1-minute historical bars to compute a true rolling 60-minute
realized volatility, intraday range, and signed return — then ranks the
universe by your chosen metric.

> **Read-only by construction.** This package never imports or invokes any
> order-placement, modification, cancellation, or account/position endpoint.
> See `tests/test_readonly.py` for the AST-level invariant check, and enable
> the **"Read-Only API"** checkbox in TWS as defense in depth.

## Install

```bash
# Option A: pip
pip install -e .[dev]

# Option B: uv
uv sync
```

Python ≥ 3.11 required.

## TWS / IB Gateway setup

1. Install **Trader Workstation** (TWS) or **IB Gateway** from
   <https://www.interactivebrokers.com/en/trading/tws.php>.
2. Use **paper trading first**. Sign into the paper account from the launcher.
3. Open **File → Global Configuration → API → Settings** and:
   - Tick **"Enable ActiveX and Socket Clients"**.
   - Tick **"Read-Only API"** (strongly recommended).
   - Confirm the **Socket port** matches what you pass to the CLI:
     - Paper TWS → `7497` (default here)
     - Live TWS → `7496`
     - Paper IB Gateway → `4002`
     - Live IB Gateway → `4001`
   - Add `127.0.0.1` to **Trusted IPs**.
4. Keep TWS / Gateway open and logged in while you run the screener.

## Market data subscriptions — caveats

You need the right subscriptions for historical TRADES bars in each market:

- **US listed + OTC top-of-book** — *US Securities Snapshot and Futures Value Bundle* (free for active traders).
- **TSX / TSX Venture** — separate Canadian subscription.
- **OTC Global Equities (ARCAEDGE)** — required for OTC trade bars. Without
  it, the screener auto-falls-back to `MIDPOINT` bars for OTC names.

Scanner rankings often work without subs; historical TRADES may not. If you
only have delayed data, pass `--allow-delayed` to switch to 15-min delayed
streaming quotes (this still works for many historical requests).

## Usage

### Show the help

```bash
ibkr-vol-screener --help
```

### One-off screen across all three markets

```bash
ibkr-vol-screener once --top-results 30
```

### US-major only, top 20 by range

```bash
ibkr-vol-screener once --markets us_major --top-results 20
```

### OTC only with stricter filters and CSV export

```bash
ibkr-vol-screener once \
  --markets otc \
  --min-price 0.50 \
  --min-volume-60m 500000 \
  --min-dollar-volume-60m 250000 \
  --csv ./out/otc_60m.csv
```

### Sort by absolute return instead of range

```bash
ibkr-vol-screener once --sort abs_return_pct
```

Other valid `--sort` keys: `range_pct` (default), `abs_return_pct`,
`volume_60m`, `dollar_volume_60m`, `realized_vol_60m`, `atr_pct_60m`,
`vwap_dev_pct` (signed, sorted by magnitude), `gap_pct` (requires `--with-gap`).

### Add gap-from-prior-close (one extra request per name)

```bash
ibkr-vol-screener once --markets us_major --with-gap --sort gap_pct
```

`--with-gap` issues an additional daily-bar request per candidate to pull the
previous session's close and report `gap_pct`. This roughly doubles the
pacing cost, so it's off by default.

### Add a rotating debug log

```bash
ibkr-vol-screener once --log-file ./run.log --verbose
```

`--log-file` attaches a `RotatingFileHandler` (10 MB × 5 backups) at DEBUG
level. The console still shows the INFO summary, but the file captures
everything including ib_async's internal chatter.

### Include the UK / EU bucket

```bash
ibkr-vol-screener once --markets us_major,uk_eu --top-results 30
```

UK/EU is opt-in (not in the default 3-bucket set). It defaults to
`STK.EU.LSE` (London — IBKR scanners aren't configured for the broad
`STK.EU` code); override via `[profile.uk_eu] location_code` in the
config to target a different venue.

### Save a snapshot for later replay

```bash
ibkr-vol-screener once --markets us_major --save-snapshot ./out/snap-001
```

`once` writes `snapshot.json`, `bars/<conId>.json`, and (if `--with-gap`)
`prior_closes.json` into the directory. `watch` does this **automatically
every cycle** under `./snapshots/<iso_timestamp>/` — pass
`--no-save-snapshot` to disable or `--snapshot-dir PATH` to relocate.

### Replay a snapshot (no IBKR connection)

```bash
ibkr-vol-screener replay ./out/snap-001 --sort atr_pct_60m --top-results 20
```

Re-runs metrics + filter + rank against the saved bars. Useful for tuning
sort keys and filter thresholds without burning the 60-req/10-min pacing
budget. Accepts most of the same `--sort`, `--min-*`, and output flags
as `once`.

### Multi-timeframe acceleration + composite score

Every metric row now carries shorter-window siblings (5m / 15m / 30m)
in addition to the canonical 60m values, plus two derived numbers:

- **`accel_factor`** = `range_pct_15m / range_pct_60m`. Values > 1
  mean the most recent 15 minutes contained more swing than the
  hour's flat-distribution baseline; a clean signal that a name is
  *heating up right now*.
- **`composite_score`** — a weighted, clipped blend of
  `range_pct`, `atr_pct_60m`, `|vwap_dev_pct|`, `accel_factor`,
  and `realized_vol_60m`. Defaults sum to 1.0; tune via TOML:

  ```toml
  [score]
  weights = { range_pct = 0.3, atr_pct_60m = 0.2,
              vwap_dev_abs = 0.15, accel_factor = 0.25,
              realized_vol_60m = 0.1 }
  ```

Both fields are sortable, filterable, and rendered in the live
table when there's space (`Score` always, `Accel` at width ≥ 100,
`R15%` at width ≥ 120). The full 5m / 15m / 30m breakdown is
written to CSV / JSON exports.

```bash
# Sort by composite score
ibkr-vol-screener once --sort composite_score --top-results 20

# Surface names whose last 15m is accelerating
ibkr-vol-screener once --sort accel_factor \
  --filter 'accel_factor > 1 AND range_pct > 1'
```

### Narrow terminals

The Rich table auto-resizes for your terminal: low-priority columns
drop out as width shrinks. Priorities (P0 always shown, P4 last to
drop):

| Width | Columns shown |
| --- | --- |
| any | `#`, `Sym`, `Bkt`, `Px`, `Range%`, `Sgn%`, `60m` (sparkline), `Bars` |
| ≥ 100 | + `|d|%`, `ATR%` |
| ≥ 120 | + `RVol%`, `VWdev%` |
| ≥ 140 | + `Vol60`, `$Vol60`, `Gap%` (when present) |
| ≥ 160 | + `Scans` |

Force a specific width with `--width N` (also `IBKR_VOL_SCREENER_WIDTH`
env var) — useful for piping to a file or CI logs that want the full
table regardless of terminal size.

### Quieter output for logs / CI

```bash
ibkr-vol-screener once --no-progress --width 200 > screen.txt
```

`--no-progress` skips the live spinner during the historical-bar
fetch. The INFO log lines remain so the run is still observable.

### Backtest: did composite_score actually predict anything?

Once you've accumulated some watch-mode history under `./snapshots/`,
run an offline backtest that walks every consecutive snapshot pair
and uses the next snapshot's bars as the "forward outcome" of the
previous snapshot's prediction.

```bash
ibkr-vol-screener backtest ./snapshots/ \
  --target forward_range_pct \
  --top-k 10
```

You'll get three tables:

1. **Per-metric correlation** — Spearman correlation between each
   tunable metric (range_pct, atr_pct_60m, vwap_dev_abs,
   accel_factor, realized_vol_60m) and the forward outcome.
2. **Top-K vs baseline** — how the top-10-by-composite-score
   actually performed (mean / median / win-rate) vs the full
   candidate universe.
3. *(optional)* **Suggested weights** with `--tune-weights`:

```bash
ibkr-vol-screener backtest ./snapshots/ --tune-weights
```

prints a paste-ready TOML block:

```toml
# Suggested by `backtest --tune-weights`.
# Derived from 84 forward outcomes. Review before committing;
# small sample sizes overfit easily.
[score]
weights = {
    range_pct = 0.3421,
    atr_pct_60m = 0.2102,
    ...
}
```

The math is naive on purpose: weights are proportional to
`|Spearman|` and normalized to sum to 1.0. It's a starting point,
not a model — the suggestion comments out a sample-size warning so
you don't trust two snapshots' worth of data.

### HK / Asia bucket

```bash
ibkr-vol-screener once --markets us_major,hk --top-results 20
```

`hk` is opt-in (not in the default 3-bucket set). Defaults to
`STK.HK.SEHK` (Hong Kong main board); override via
`[profile.hk] location_code = "STK.HK.TSE_JPN"` (Japan) or
`"STK.HK.SEHKNTL"` (Shanghai-HK connect) in the config.

Note: paper accounts typically lack HK scanner subscriptions, so
the bucket may gracefully drop with a clear log message — same UX
as the UK/EU bucket.

### Watchlist: track your own symbols alongside scanners

Create a file with one symbol per line (`#` comments and blank lines
are ignored), then point `once` or `watch` at it:

```
# my-tickers.txt
AAPL
MSFT
NVDA
TSLA
```

```bash
ibkr-vol-screener once --watchlist ./my-tickers.txt --markets us_major
```

Watchlist symbols are qualified via `reqContractDetails` and added
to the candidate pool deduped against scanner output. They get
`source_scan_codes={"WATCHLIST"}` so the analytics churn / scan-code
predictiveness tables can tell them apart.

### Composable filter expressions

```bash
ibkr-vol-screener once \
  --filter '(range_pct>3 AND volume_60m>5e5) OR atr_pct_60m>5'
```

Grammar: parens + `AND`/`OR` (case-insensitive) + comparisons
(`>`, `>=`, `<`, `<=`, `==`, `!=`). Multiple `--filter` flags
combine with implicit AND. Allowed metric names match `--sort`'s
plus `first_open`, `last_close`, `high_60m`, `low_60m`, `n_bars`.
None-valued attributes (`gap_pct` without `--with-gap`) evaluate
as False, never crash.

### Render a chart of a saved snapshot

```bash
# Install matplotlib extra once
pip install ibkr-vol-screener[charts]

# Single snapshot -> grid of 60m close-price line plots
ibkr-vol-screener chart ./snapshots/2026-05-19T15-30-00 \
    --top-k 8 --out ./out/chart.png

# All cycles in a snapshots root -> multi-line metric history
ibkr-vol-screener chart ./snapshots --history \
    --metric range_pct --top-k 6 --out ./out/history.svg
```

Pass `--show` to also open the figure in a GUI viewer (Tk on most
Windows installs).

### Analyze accumulated watch history

```bash
ibkr-vol-screener analyze ./snapshots/ \
  --sort range_pct \
  --top-k 10 \
  --metric atr_pct_60m \
  --csv ./out/history.csv
```

Walks every snapshot directory under the given root (these are written
by `watch` automatically) and prints four tables:

1. **Run overview** — cycles found, time span, unique symbols seen.
2. **Top-K churn** — which symbols keep appearing in the top-K by
   `--sort` and what fraction of cycles they show up in.
3. **Persistent symbols** — for symbols seen in ≥ 25% of cycles, the
   min/median/max of `--metric` plus a sparkline of that metric over
   time.
4. **Scan-code predictiveness** — mean of `--metric` per IBKR scan code
   (HOT_BY_VOLUME, TOP_PERC_GAIN, …). Tells you whether the IBKR
   scanner is actually surfacing names that turn out to be volatile.

`--csv out.csv` exports a long-form `(ts, symbol, bucket, metric,
value)` file for further analysis in a spreadsheet or notebook.

### Threshold alerts in watch mode

```bash
ibkr-vol-screener watch \
  --interval 60 \
  --alert-range-pct 3.0 \
  --alert-abs-return-pct 2.0 \
  --alerts-log ./alerts.log
```

When a row's metric crosses the threshold, its row turns bold red in
the live table and a structured line is appended to `alerts.log`:

```
2026-05-19T18:01:32+00:00 SOXS us_major range_pct>=3 value=4.0673
```

Each breach fires **once** — the symbol re-arms only after dropping
below 80% of the threshold (hysteresis to avoid spam). Multiple
`--alert-*` flags can be combined; `--alert-vwap-dev-pct` and
`--alert-gap-pct` compare the absolute magnitude.

### Refresh every 60 s with live table

```bash
ibkr-vol-screener watch --interval 60 --markets us_major,tsx,otc
```

**Streaming** is the default — `watch` holds the IB connection open
and subscribes to `reqRealTimeBars` per candidate. The first time a
candidate appears it gets a one-shot `reqHistoricalData` backfill;
after that, the rolling buffer fills from live 5-second bars and no
further historical fetches are needed. Avoids the per-cycle pacing
pressure of the legacy approach at high refresh rates.

Pass `--no-streaming` to fall back to the original per-cycle
`reqHistoricalData` flow. Cap the number of simultaneous subscriptions
with `--streaming-max-subs N` (default 100).

### Discover scanner locations / scan codes (helpful for TSX)

```bash
ibkr-vol-screener scanner-params
```

This downloads + caches the scanner XML and prints all `STK.*` location
codes and `<ScanType>` codes available to your account. Useful to find
the exact Canada/TSX location string for your region.

### Print an example config

```bash
ibkr-vol-screener config-example > screener.toml
ibkr-vol-screener once --config screener.toml
```

## How the volatility metric works

For each candidate, the screener pulls a 90-minute buffer of 1-minute bars
and slices to the last 60 minutes. From that window it computes:

| Field | Definition |
| --- | --- |
| `range_pct` | `(high_60m - low_60m) / last_close * 100` — intraday high-low range as a percent of the latest close |
| `signed_return_pct` | `(last_close - first_open) / first_open * 100` |
| `abs_return_pct` | `\|signed_return_pct\|` |
| `realized_vol_60m` | annualized stdev of 1-min log returns, in % |
| `atr_pct_60m` | mean true-range over 1-min bars (incl. close-to-close gaps) as % of `last_close` |
| `vwap_60m`, `vwap_dev_pct` | volume-weighted average price over the window (typical-price basis); `vwap_dev_pct` is `(last_close - vwap) / vwap * 100` |
| `gap_pct` | `(first_open_60m - prior_close) / prior_close * 100`; only populated when `--with-gap` was passed |
| `volume_60m`, `dollar_volume_60m` | sums over the 60-minute window (UK/EU values are in local currency, no FX conversion) |

`range_pct` is the **default sort key** — it captures *intra-window
swing* even when the net move is small.

## Scanner results are candidates, not the answer

IBKR's `HOT_BY_VOLUME`, `TOP_PERC_GAIN`, etc. rank on full-session or
open-relative metrics, not on rolling 60-minute volatility. The screener
uses them only to surface a plausible candidate pool. The **true ranking is
re-computed** from the 1-minute bars over the last 60 minutes.

## Troubleshooting

- **Cannot connect to TWS** — confirm TWS / Gateway is logged in, API
  settings enabled, port matches, and `127.0.0.1` is in the Trusted IP list.
  Try a different `--client-id` if another script is already connected with
  the same ID.
- **Invalid scanner location** — run
  `ibkr-vol-screener scanner-params --refresh` and copy the exact code
  into `[profile.<bucket>] location_code = "..."` in your config.
- **No TSX location found** — set `[profile.tsx] location_code` explicitly
  in the config (see `config-example`).
- **UK/EU scanner returns 0 candidates / "Market Scanner is not configured
  for one of the chosen locations"** — IBKR error 365. The account doesn't
  have EU scanner subscriptions. This is common on paper accounts. Either
  subscribe to the relevant European data feed in IBKR Client Portal, or
  drop `uk_eu` from `--markets`. The screener exits the bucket gracefully
  with `0 unique candidates` and continues with the other markets.
- **Market data not subscribed** — historical TRADES will be empty or
  error 165. Either subscribe, drop the affected market, or use
  `--allow-delayed`.
- **Historical pacing violation (error 162)** — the screener retries with
  exponential backoff. To prevent it, lower `--top-candidates-per-scan` or
  reduce `concurrency` / increase `request_delay_s` in the config.
- **No bars returned** — for OTC the tool auto-retries with
  `whatToShow=MIDPOINT`. For other markets, check that TWS is logged in to
  the right account (paper vs live) and that the market is open or
  recently closed.
- **Delayed vs real-time data** — `--allow-delayed` switches to 15-min
  delayed mode. The most recent ~15 minutes of bars may be missing; older
  bars usually work.

## Development

```bash
# install dev deps
pip install -e .[dev]

# format + lint
ruff format src tests
ruff check src tests

# tests (no IBKR required)
pytest -q

# integration smoke test (needs TWS at 127.0.0.1:7497)
IBKR_LIVE_TEST=1 pytest tests/test_live.py
```

See `docs/research_notes.md` for the IBKR API specifics this code relies on,
and `tests/test_readonly.py` for the read-only invariant check.
