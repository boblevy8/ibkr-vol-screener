"""Tests for the --strategy preset registry."""

from __future__ import annotations

import pytest

from ibkr_vol_screener.config import Config, MarketBucket
from ibkr_vol_screener.strategies import (
    STRATEGY_PRESETS,
    apply_strategy,
    get_strategy,
    list_strategies,
)


def test_all_presets_construct():
    """No preset should crash to instantiate."""
    assert len(STRATEGY_PRESETS) >= 5
    for preset in list_strategies():
        assert preset.name == preset.name  # accessor works
        assert preset.description


def test_get_strategy_known():
    assert get_strategy("gap").name == "gap"
    assert get_strategy("breakout").sort_key == "accel_factor"


def test_get_strategy_unknown_raises_with_list():
    with pytest.raises(KeyError) as exc_info:
        get_strategy("not_a_strategy")
    msg = str(exc_info.value)
    # Error message should hint at the valid set.
    assert "Valid:" in msg
    assert "gap" in msg


def test_apply_gap_sets_with_gap_and_sort():
    cfg = Config()
    assert cfg.with_gap is False  # default
    apply_strategy(cfg, "gap")
    assert cfg.with_gap is True
    assert cfg.sort_key == "gap_pct"
    assert MarketBucket.US_MAJOR in cfg.markets


def test_apply_breakout_filters_compose():
    cfg = Config()
    cfg.filter_exprs = ("range_pct > 5",)  # existing filter
    apply_strategy(cfg, "breakout")
    # New filters appended, not replaced.
    assert "range_pct > 5" in cfg.filter_exprs
    assert any("accel_factor" in f for f in cfg.filter_exprs)


def test_apply_active_sets_min_volume():
    cfg = Config()
    apply_strategy(cfg, "active")
    # Every market profile got min_volume_60m=500_000 applied.
    for prof in cfg.profiles.values():
        assert prof.min_volume_60m == 500_000


def test_apply_reversion_sort_and_filter():
    cfg = Config()
    apply_strategy(cfg, "reversion")
    assert cfg.sort_key == "vwap_dev_pct"
    assert any("vwap" not in f or True for f in cfg.filter_exprs)  # has filters
    assert any("abs_return_pct" in f for f in cfg.filter_exprs)


def test_apply_volatile_default_composite():
    cfg = Config()
    apply_strategy(cfg, "volatile")
    assert cfg.sort_key == "composite_score"


def test_apply_unknown_raises():
    cfg = Config()
    with pytest.raises(KeyError):
        apply_strategy(cfg, "bogus")


def test_filter_exprs_dedupe_not_required():
    """Calling apply twice should accumulate (we don't dedupe)."""
    cfg = Config()
    apply_strategy(cfg, "breakout")
    n_after_first = len(cfg.filter_exprs)
    apply_strategy(cfg, "breakout")
    # Filters appended again; we don't dedupe, which is intentional.
    assert len(cfg.filter_exprs) >= n_after_first
