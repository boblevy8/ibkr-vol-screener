"""Pre-canned strategy presets that bundle markets + sort + filters.

Each preset answers a specific question ("what gapped this morning?",
"what's accelerating into the close?") with one CLI flag rather than
forcing the user to compose `--sort + --filter + --markets +
--with-gap` from scratch every time.

Presets layer onto `Config`; explicit per-flag CLI overrides still
win (they're applied by the CLI *after* `apply_strategy`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from .config import Config, MarketBucket

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategyPreset:
    name: str
    description: str
    markets: tuple[MarketBucket, ...] | None  # None -> don't override
    sort_key: str | None
    filter_exprs: tuple[str, ...]
    with_gap: bool = False
    min_price: float | None = None
    min_volume_60m: float | None = None
    min_dollar_volume_60m: float | None = None


STRATEGY_PRESETS: dict[str, StrategyPreset] = {
    "volatile": StrategyPreset(
        name="volatile",
        description=(
            "Composite-score sort with a soft floor on range. Same shape as "
            "plain `once` but explicit about the ranking."
        ),
        markets=(MarketBucket.US_MAJOR, MarketBucket.TSX, MarketBucket.OTC),
        sort_key="composite_score",
        filter_exprs=("range_pct > 1",),
    ),
    "gap": StrategyPreset(
        name="gap",
        description=(
            "Morning gappers: sort by signed gap_pct magnitude. Requires "
            "--with-gap which is auto-enabled by the preset."
        ),
        markets=(MarketBucket.US_MAJOR, MarketBucket.OTC),
        sort_key="gap_pct",
        filter_exprs=("abs_return_pct > 1",),
        with_gap=True,
    ),
    "breakout": StrategyPreset(
        name="breakout",
        description=(
            "Recent acceleration: surfaces names whose last 15 minutes "
            "outpace the hour's average minute."
        ),
        markets=(MarketBucket.US_MAJOR,),
        sort_key="accel_factor",
        filter_exprs=("accel_factor > 1 AND range_pct > 1.5",),
    ),
    "reversion": StrategyPreset(
        name="reversion",
        description=(
            "Mean-reversion candidates: large move + far-from-VWAP. Sort "
            "by absolute VWAP deviation."
        ),
        markets=(MarketBucket.US_MAJOR,),
        sort_key="vwap_dev_pct",
        filter_exprs=("abs_return_pct > 2 AND atr_pct_60m > 1.5",),
    ),
    "active": StrategyPreset(
        name="active",
        description=(
            "Most heavily traded names. Pure $-volume ranking with a "
            "1M-shares floor."
        ),
        markets=(MarketBucket.US_MAJOR, MarketBucket.OTC),
        sort_key="dollar_volume_60m",
        filter_exprs=("volume_60m > 1e6",),
        min_volume_60m=500_000,
    ),
}


def list_strategies() -> list[StrategyPreset]:
    return list(STRATEGY_PRESETS.values())


def get_strategy(name: str) -> StrategyPreset:
    try:
        return STRATEGY_PRESETS[name]
    except KeyError as exc:
        valid = ", ".join(sorted(STRATEGY_PRESETS))
        raise KeyError(
            f"Unknown strategy {name!r}. Valid: {valid}"
        ) from exc


def apply_strategy(cfg: Config, name: str) -> Config:
    """Mutate `cfg` in place with the named preset's defaults.

    Returns the same Config for chaining. Filter expressions are
    *appended* to any existing `cfg.filter_exprs` so users can compose
    `--strategy breakout --filter 'volume_60m > 2e6'`.

    Per-market thresholds (min_price etc.) overwrite the existing
    profile values uniformly across all profiles, mirroring how the
    --min-price CLI flag is applied.
    """
    preset = get_strategy(name)
    log.info("Applying strategy preset: %s", preset.name)

    if preset.markets is not None:
        cfg.markets = preset.markets
    if preset.sort_key is not None:
        cfg.sort_key = preset.sort_key
    if preset.with_gap:
        cfg.with_gap = True
    if preset.filter_exprs:
        cfg.filter_exprs = tuple(list(cfg.filter_exprs) + list(preset.filter_exprs))

    threshold_overrides = {
        k: v
        for k, v in (
            ("min_price", preset.min_price),
            ("min_volume_60m", preset.min_volume_60m),
            ("min_dollar_volume_60m", preset.min_dollar_volume_60m),
        )
        if v is not None
    }
    if threshold_overrides:
        for bucket, prof in cfg.profiles.items():
            cfg.profiles[bucket] = replace(prof, **threshold_overrides)

    return cfg
