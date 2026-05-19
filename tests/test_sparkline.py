"""Tests for the sparkline helper."""

from __future__ import annotations

from ibkr_vol_screener.reporting import sparkline


def test_empty_input_returns_empty_string():
    assert sparkline([]) == ""


def test_flat_input_returns_horizontal_line():
    assert sparkline([1.0, 1.0, 1.0, 1.0]) == "─" * 10
    assert sparkline([5.5] * 20, width=5) == "─" * 5


def test_width_param_honored():
    assert len(sparkline([1.0, 2.0, 3.0, 4.0], width=8)) == 8
    assert len(sparkline(list(range(100)), width=15)) == 15


def test_monotonic_increasing_ends_with_full_block():
    s = sparkline([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    # Last bucket contains the max -> should be the topmost block character.
    assert s[-1] == "█"
    # First bucket contains the min -> bottommost block.
    assert s[0] == "▁"


def test_monotonic_decreasing_ends_with_lowest_block():
    s = sparkline([10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0])
    assert s[0] == "█"
    assert s[-1] == "▁"


def test_handles_negative_values():
    # Bucket averages still make sense with negatives.
    s = sparkline([-1.0, 0.0, 1.0, 2.0])
    assert len(s) == 10
    assert s.count("─") == 0  # not flat


def test_zero_width_returns_empty():
    assert sparkline([1.0, 2.0, 3.0], width=0) == ""
