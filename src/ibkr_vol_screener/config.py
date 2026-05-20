"""Configuration model and TOML loader.

Defines the three market profiles (US major, TSX, OTC), default scan codes,
filters, pacing knobs, and a `Config` dataclass that the CLI populates from
a combination of TOML file + CLI overrides.

The default scan codes here are taken from publicly documented IBKR codes.
At runtime, `scanner_params.parse_scan_codes()` is used to validate which
of these are actually available — missing codes are skipped with a warning.
"""

from __future__ import annotations

import enum
import logging
import tomllib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class MarketBucket(enum.StrEnum):
    US_MAJOR = "us_major"
    TSX = "tsx"
    OTC = "otc"
    UK_EU = "uk_eu"


# Default scan codes per market. These are validated against the live scanner
# XML at runtime (scanner_params.parse_scan_codes); missing codes are skipped.
# See docs/research_notes.md for source references.
_DEFAULT_US_MAJOR_SCANS = (
    "HOT_BY_VOLUME",
    "TOP_PERC_GAIN",
    "TOP_PERC_LOSE",
    "MOST_ACTIVE",
    "TOP_TRADE_COUNT",
)
_DEFAULT_OTC_SCANS = (
    "HOT_BY_VOLUME",
    "TOP_PERC_GAIN",
    "TOP_PERC_LOSE",
    "MOST_ACTIVE",
)
_DEFAULT_TSX_SCANS = (
    "HOT_BY_VOLUME",
    "TOP_PERC_GAIN",
    "TOP_PERC_LOSE",
    "MOST_ACTIVE",
)
_DEFAULT_UK_EU_SCANS = (
    "HOT_BY_VOLUME",
    "TOP_PERC_GAIN",
    "TOP_PERC_LOSE",
    "MOST_ACTIVE",
)


@dataclass(frozen=True)
class MarketProfile:
    bucket: MarketBucket
    location_code: str | None  # None for TSX -> discover at runtime
    scan_codes: tuple[str, ...]
    min_price: float
    min_volume_60m: float
    min_dollar_volume_60m: float
    max_price: float | None = None


def default_profile(bucket: MarketBucket) -> MarketProfile:
    if bucket is MarketBucket.US_MAJOR:
        return MarketProfile(
            bucket=bucket,
            location_code="STK.US.MAJOR",
            scan_codes=_DEFAULT_US_MAJOR_SCANS,
            min_price=1.0,
            min_volume_60m=50_000,
            min_dollar_volume_60m=250_000,
        )
    if bucket is MarketBucket.OTC:
        # Stricter defaults: pinks are noisy + thinly traded.
        return MarketProfile(
            bucket=bucket,
            location_code="STK.US.MINOR",
            scan_codes=_DEFAULT_OTC_SCANS,
            min_price=0.10,
            min_volume_60m=200_000,
            min_dollar_volume_60m=100_000,
        )
    if bucket is MarketBucket.TSX:
        # location_code resolved at runtime via scanner_params.
        return MarketProfile(
            bucket=bucket,
            location_code=None,
            scan_codes=_DEFAULT_TSX_SCANS,
            min_price=0.50,
            min_volume_60m=50_000,
            min_dollar_volume_60m=100_000,
        )
    # UK_EU: location_code resolved at runtime; thresholds in local currency.
    return MarketProfile(
        bucket=bucket,
        location_code=None,
        scan_codes=_DEFAULT_UK_EU_SCANS,
        min_price=1.0,
        min_volume_60m=20_000,
        min_dollar_volume_60m=50_000,
    )


@dataclass
class Config:
    # Connection.
    host: str = "127.0.0.1"
    port: int = 7497  # paper TWS; 7496 for live, 4001/4002 for IB Gateway
    client_id: int = 42

    # Markets. UK_EU is opt-in (not in the default tuple).
    markets: tuple[MarketBucket, ...] = (
        MarketBucket.US_MAJOR,
        MarketBucket.TSX,
        MarketBucket.OTC,
    )
    profiles: dict[MarketBucket, MarketProfile] = field(
        default_factory=lambda: {b: default_profile(b) for b in MarketBucket}
    )

    # Candidate generation.
    top_candidates_per_scan: int = 25
    max_candidates_per_market: int = 200  # hard cap for pacing budget

    # Ranking + display.
    top_results: int = 30
    sort_key: str = "range_pct"  # range_pct|abs_return_pct|volume_60m|dollar_volume_60m

    # Historical fetch.
    use_rth: bool = False
    duration_seconds: int = 5400  # 90 minutes
    bar_size: str = "1 min"
    what_to_show: str = "TRADES"
    min_bars_for_metric: int = 10
    concurrency: int = 4
    request_delay_s: float = 0.25
    max_retries: int = 3

    # Optional market-data fallback (3 = delayed, 1 = live).
    market_data_type: int = 1
    allow_delayed: bool = False

    # Phase 2 flags.
    with_gap: bool = False  # fetch prior session close to compute gap_pct
    log_file: Path | None = None  # rotating log file path

    # Phase 3: snapshot for replay.
    save_snapshot_dir: Path | None = None  # explicit root for once/watch

    # Phase 5: watchlist + composable filters.
    watchlist: tuple[str, ...] = ()  # explicit symbol list (e.g. from CLI)
    watchlist_file: Path | None = None  # alternative: path to a file
    filter_exprs: tuple[str, ...] = ()  # raw --filter expressions (AND-combined)

    # Phase 6: multi-timeframe + composite score.
    window_minutes: tuple[int, ...] = (60, 30, 15, 5)
    min_bars_short: int = 3
    score_weights: dict[str, float] = field(
        default_factory=lambda: _default_score_weights()
    )

    # Outputs.
    csv_path: Path | None = None
    json_path: Path | None = None
    html_path: Path | None = None

    # Misc.
    verbose: bool = False
    dry_run: bool = False
    cache_dir: Path = field(default_factory=lambda: _default_cache_dir())
    scanner_xml_max_age_h: float = 24.0


def _default_cache_dir() -> Path:
    return Path.home() / ".cache" / "ibkr_vol_screener"


def _default_score_weights() -> dict[str, float]:
    # Late-imported to avoid a circular dep: metrics.py imports config.py.
    from .metrics import DEFAULT_SCORE_WEIGHTS

    return dict(DEFAULT_SCORE_WEIGHTS)


def _coerce_markets(value: Any) -> tuple[MarketBucket, ...]:
    if isinstance(value, str):
        items = [s.strip() for s in value.split(",") if s.strip()]
    else:
        items = list(value)
    out: list[MarketBucket] = []
    for item in items:
        if isinstance(item, MarketBucket):
            out.append(item)
        else:
            out.append(MarketBucket(item.lower()))
    return tuple(out)


def load_config(path: Path | None = None) -> Config:
    """Load Config from an optional TOML file. Missing file -> defaults."""
    cfg = Config()
    if path is None:
        return cfg
    if not path.exists():
        log.warning("Config file %s not found; using defaults.", path)
        return cfg
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    conn = data.get("connection", {})
    if "host" in conn:
        cfg.host = str(conn["host"])
    if "port" in conn:
        cfg.port = int(conn["port"])
    if "client_id" in conn:
        cfg.client_id = int(conn["client_id"])

    screen = data.get("screen", {})
    if "markets" in screen:
        cfg.markets = _coerce_markets(screen["markets"])
    for k in (
        "top_candidates_per_scan",
        "max_candidates_per_market",
        "top_results",
        "min_bars_for_metric",
        "concurrency",
        "max_retries",
        "duration_seconds",
        "market_data_type",
    ):
        if k in screen:
            setattr(cfg, k, int(screen[k]))
    for k in ("request_delay_s",):
        if k in screen:
            setattr(cfg, k, float(screen[k]))
    for k in ("use_rth", "allow_delayed", "verbose"):
        if k in screen:
            setattr(cfg, k, bool(screen[k]))
    for k in ("sort_key", "what_to_show", "bar_size"):
        if k in screen:
            setattr(cfg, k, str(screen[k]))
    if "watchlist" in screen:
        cfg.watchlist = tuple(str(s).upper() for s in screen["watchlist"])
    if "watchlist_file" in screen:
        cfg.watchlist_file = Path(str(screen["watchlist_file"]))

    # Score weights override.
    score = data.get("score", {})
    if isinstance(score, dict):
        weights_in = score.get("weights")
        if isinstance(weights_in, dict):
            valid_keys = set(cfg.score_weights)
            for k, v in weights_in.items():
                if k not in valid_keys:
                    log.warning("Ignoring unknown score weight %r", k)
                    continue
                cfg.score_weights[k] = float(v)

    # Optional per-market profile overrides.
    for bucket in MarketBucket:
        section = data.get(f"profile.{bucket.value}", None) or data.get(
            "profile", {}
        ).get(bucket.value)
        if not section:
            continue
        base = cfg.profiles[bucket]
        kwargs: dict[str, Any] = {}
        for k in (
            "location_code",
            "min_price",
            "min_volume_60m",
            "min_dollar_volume_60m",
            "max_price",
        ):
            if k in section:
                kwargs[k] = section[k]
        if "scan_codes" in section:
            kwargs["scan_codes"] = tuple(section["scan_codes"])
        cfg.profiles[bucket] = replace(base, **kwargs)

    return cfg


def apply_cli_overrides(cfg: Config, **overrides: Any) -> Config:
    """Apply non-None CLI overrides onto a Config. Returns the same instance."""
    for k, v in overrides.items():
        if v is None:
            continue
        if k == "markets":
            cfg.markets = _coerce_markets(v)
            continue
        if not hasattr(cfg, k):
            log.debug("Unknown override %s=%r ignored", k, v)
            continue
        setattr(cfg, k, v)
    return cfg


def example_config_toml() -> str:
    """A commented TOML example with all currently supported keys."""
    return """# ibkr-vol-screener configuration

[connection]
host = "127.0.0.1"
port = 7497          # 7497 = paper TWS, 7496 = live TWS, 4002 = paper IB Gateway, 4001 = live
client_id = 42

[screen]
markets = "us_major,tsx,otc"     # comma list of buckets: us_major, tsx, otc
top_candidates_per_scan = 25     # candidates pulled per IBKR scan code
max_candidates_per_market = 200  # hard cap to respect IBKR pacing (60 req / 10 min)
top_results = 30                 # rows displayed
sort_key = "range_pct"           # range_pct | abs_return_pct | volume_60m | dollar_volume_60m
use_rth = false                  # true = regular hours only; false = include pre/post market
duration_seconds = 5400          # 90 min buffer; we slice the last 60 min from this
min_bars_for_metric = 10         # skip rows with fewer 1-min bars
concurrency = 4                  # bounded concurrent historical-data requests
request_delay_s = 0.25           # min sleep between requests
max_retries = 3                  # retries on pacing/transient errors
market_data_type = 1             # 1 = live, 3 = delayed (if allow_delayed)
allow_delayed = false
verbose = false

# Optional per-market profile overrides.
[profile.us_major]
location_code = "STK.US.MAJOR"
scan_codes = ["HOT_BY_VOLUME", "TOP_PERC_GAIN", "TOP_PERC_LOSE", "MOST_ACTIVE"]
min_price = 1.0
min_volume_60m = 50000
min_dollar_volume_60m = 250000

[profile.tsx]
# Leave location_code unset to auto-discover from scanner XML.
# Set explicitly e.g. "STK.NA.CANADA" or "STK.NA.TSE" to override.
# location_code = "STK.NA.CANADA"
scan_codes = ["HOT_BY_VOLUME", "TOP_PERC_GAIN", "TOP_PERC_LOSE", "MOST_ACTIVE"]
min_price = 0.50
min_volume_60m = 50000
min_dollar_volume_60m = 100000

[profile.otc]
location_code = "STK.US.MINOR"
scan_codes = ["HOT_BY_VOLUME", "TOP_PERC_GAIN", "TOP_PERC_LOSE", "MOST_ACTIVE"]
min_price = 0.10
min_volume_60m = 200000
min_dollar_volume_60m = 100000
"""


def config_summary(cfg: Config) -> dict[str, Any]:
    """JSON-serializable summary for logging / --verbose output."""
    d = asdict(cfg)
    d["markets"] = [m.value for m in cfg.markets]
    d["profiles"] = {
        b.value: {**asdict(p), "bucket": b.value, "scan_codes": list(p.scan_codes)}
        for b, p in cfg.profiles.items()
    }
    for k in ("csv_path", "json_path", "html_path", "cache_dir"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d
