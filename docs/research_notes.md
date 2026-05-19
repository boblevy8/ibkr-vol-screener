# IBKR TWS API research notes — `ibkr-vol-screener`

These notes were captured during the research phase for the
`ibkr-vol-screener` project. They cite primary sources and call out
uncertainties so future maintainers can verify rather than guess.

## Library: `ib_async`

- Active fork of the archived `ib_insync`. Same public API; same import surface.
- Repo: <https://github.com/ib-api-reloaded/ib_async>
- PyPI: <https://pypi.org/project/ib-async/>
- Docs: <https://ib-api-reloaded.github.io/ib_async/>
- Python requirement: `>= 3.10`. We target `>= 3.11` for `tomllib` and modern typing.
- Main class: `from ib_async import IB`. Async-first; sync helpers also exist but we use async exclusively.

Why this over `ib_insync`? The original maintainer (Ewald de Wit) passed
away in early 2024 and the project lost active stewardship. `ib_async` is
maintained by Matt Stancliff et al.

## Scanner API

Two relevant endpoints:

| Endpoint | Returns | Used here |
| --- | --- | --- |
| `IB.reqScannerParametersAsync()` | XML string describing all valid `<Location>` + `<ScanType>` + filter `<AbstractField>`s | once per session (cached on disk for 24h) |
| `IB.reqScannerDataAsync(sub, [], filters)` | List of `ScanData` rows (each has `.contractDetails.contract`, `.rank`, `.distance`, etc.) | per scan code per market |

`ScannerSubscription` fields we set:

- `instrument="STK"`
- `locationCode=<see below>`
- `scanCode=<see below>`
- `numberOfRows=<top_n_per_scan>`

We pass additional filters via `TagValue` list — typically
`TagValue("priceAbove", "1")` etc. The full list of tag names is in the
`AbstractField` entries inside the scanner XML.

Source: <https://interactivebrokers.github.io/tws-api/market_scanners.html>

## Location codes

| Code | Universe | Confirmed? |
| --- | --- | --- |
| `STK.US.MAJOR` | US listed (NYSE / NASDAQ / AMEX / ARCA / BATS) | ✅ documented in IBKR samples |
| `STK.US.MINOR` | US OTC / Pink Sheets / OTCBB | ✅ documented in IBKR samples |
| `STK.NASDAQ`, `STK.NYSE`, `STK.AMEX`, `STK.ARCA`, `STK.BATS`, `STK.PINK`, `STK.OTCBB` | venue-specific | ✅ |
| `STK.NA.CANADA`, `STK.NA.TSE`, `STK.NA.VENTURE` | Canada / TSX | ✅ confirmed via live TWS XML (2026-05-19) |
| `STK.EU`, `STK.EU.LSE`, `STK.EU.IBIS`, `STK.EU.AEB`, `STK.EU.BVME`, `STK.EU.EBS`, ... | UK / EU | ✅ confirmed via live TWS XML (2026-05-19); 25+ codes total |

**TSX uncertainty (RESOLVED)**: A live `reqScannerParameters()` against paper
TWS on 2026-05-19 confirmed `STK.NA.CANADA` (display "Canada"), `STK.NA.TSE`
("TSE"), `STK.NA.VENTURE` ("VENTURE") all exist. The resolver still does
runtime discovery + probe so the tool stays robust if IBKR ever renames them.

**UK/EU codes confirmed via the same probe**: `STK.EU` (catch-all "Europe
Stocks"), `STK.EU.LSE` ("United Kingdom (LSE)"), `STK.EU.IBIS` ("Germany"),
`STK.EU.IBIS-XETRA`, `STK.EU.AEB` ("Netherlands"), `STK.EU.SBF` ("France"),
`STK.EU.BVME` ("Italy"), `STK.EU.EBS` ("Switzerland"), plus many country
codes. The screener defaults to `STK.EU` (broadest); override via
`[profile.uk_eu] location_code` to target a single venue.

## Useful stock scan codes

Documented and commonly available codes (validate at runtime):

- `HOT_BY_VOLUME`, `HOT_BY_PRICE`
- `TOP_PERC_GAIN`, `TOP_PERC_LOSE`
- `MOST_ACTIVE`
- `TOP_TRADE_COUNT`, `TOP_TRADE_RATE`
- `TOP_PRICE_RANGE`, `HOT_BY_PRICE_RANGE`
- `HIGH_OPEN_GAP`, `LOW_OPEN_GAP`
- `HIGH_VS_13W_HL`, `LOW_VS_13W_HL`

Availability varies by `locationCode`. The screener checks each scan
code against the XML before issuing the request and skips with a
warning if missing (`scanners.gather_candidates`).

## Historical data pacing

Source: <https://interactivebrokers.github.io/tws-api/historical_limitations.html>

Rules to respect:

- **No identical historical request within 15 seconds** for the same
  Contract/Exchange/TickType.
- **≤ 6 same-contract requests within 2 seconds.**
- **≤ 60 historical requests in any rolling 10 minutes.** `BID_ASK`
  counts as 2 requests.
- **≤ 50 simultaneous open historical requests.**

Error codes that surface pacing violations: **162** (`Historical Market
Data Service error`) and **366** (transient on busy gateways). **165** =
"no market-data permissions for this security"; not retryable.

Concretely:

- We default `concurrency=4` (well under 50 + 6).
- We default `request_delay_s=0.25` between issuances, which gives us
  roughly `≤ 4 req/s ≈ 240/min` peak — but the bounded concurrency keeps
  open requests low, and our `top_candidates_per_scan=25 × 5 scans × 3 markets ≈ 375`
  rarely materializes after dedupe (typical: 100–200 unique).
- For very large universes, drop `--top-candidates-per-scan` to stay
  inside the 60 / 10-min ceiling.

## 1-minute bar requests

```python
bars = await ib.reqHistoricalDataAsync(
    contract,
    endDateTime="",          # = now
    durationStr="5400 S",    # 90 min buffer; slice last 60 min in metrics.py
    barSizeSetting="1 min",
    whatToShow="TRADES",     # OTC falls back to MIDPOINT in historical.fetch_bars
    useRTH=False,            # default: include pre/post market
    formatDate=2,            # epoch seconds; we normalize in metrics._to_datetime
)
```

OTC caveat: `TRADES` bars for pinks are frequently empty without an
ARCAEDGE / OTC Global Equities subscription. Our fetcher transparently
retries with `MIDPOINT` once for OTC candidates.

Source: <https://interactivebrokers.github.io/tws-api/historical_bars.html>

## Read-only safety

The screener never imports nor calls any of:

- `placeOrder`, `cancelOrder`, `reqGlobalCancel`
- `reqAccountSummary`, `reqAccountUpdates`, `reqPositions`, `reqPnL`
- `transmit`-style flags

Defense in depth (recommended): in TWS / IB Gateway, open
**File → Global Configuration → API → Settings** and tick
**"Read-Only API"**. The server then refuses any order from this client
ID regardless of code. The AST check in `tests/test_readonly.py`
enforces the source-side invariant at CI time.

## Market data subscriptions

- **US Securities Snapshot and Futures Value Bundle** — covers US listed
  + OTC top-of-book. Sufficient for scanners and most US historical
  TRADES.
- **Canadian TSX / TSX Venture** — separate subscription; required for
  TSX TRADES bars.
- **OTC Global Equities / ARCAEDGE** — required for OTC TRADES bars
  (we fall back to MIDPOINT when missing).

Without subscriptions, scanner *rankings* often still come back, but
historical TRADES requests return empty / error 165. The screener logs
these clearly and continues.

`reqMarketDataType(3)` switches to **15-minute delayed** streaming
quotes. Behavior of delayed historical bars varies per venue — typically
the most recent ~15 minutes are unavailable; older bars work.

## Phase 2 additions

### Scanner-cancel error filtering
Each `reqScannerDataAsync(...)` call emits an `errorEvent` with code 162 and
text "API scanner subscription cancelled: N" when the subscription is torn
down. This is a normal completion receipt, not an actionable failure.
`observability.is_scanner_cancel_receipt()` filters these from the console;
they remain at DEBUG in the rotating log file.

### Gap pacing impact
`--with-gap` issues `reqHistoricalDataAsync(durationStr="3 D",
barSizeSetting="1 day", whatToShow="TRADES", useRTH=True)` per candidate.
That doubles the per-screen historical-request count, so the same
`max_candidates_per_market` cap should be halved if you're running close to
the 60-req / 10-min ceiling.

### Subscription error codes captured by `SubscriptionErrorTracker`
- 354, 10089, 10090, 10091 — "market data not subscribed" variants
- 10168 — "delayed market data is available"

These map to per-symbol entries; at end of run the CLI prints a single
yellow summary line listing the affected symbols.

## Open uncertainties

- Whether IBKR reports "lot volume" vs raw share volume for US TRADES
  bars depends on the data feed. CSV exports include raw `volume_60m`
  so users can spot-check.
- Some IBKR servers omit the `<Instrument>` list inside `<Location>`
  nodes — `scanner_params.stock_locations()` defensively also accepts
  any code prefixed with `STK`.
- UK/EU `volume_60m` and `dollar_volume_60m` are reported in **local
  currency** — there's no FX conversion. Filters applied to the UK/EU
  bucket use local-currency thresholds (looser defaults in
  `default_profile(MarketBucket.UK_EU)`).
