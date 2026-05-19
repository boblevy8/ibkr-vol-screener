"""AST-level invariant check: this package must never call any IBKR endpoint
that places, modifies, cancels, or inspects orders / accounts / positions."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src" / "ibkr_vol_screener"

# Methods we must never call on an IB instance (or anywhere).
_FORBIDDEN = frozenset(
    {
        "placeOrder",
        "placeOrderAsync",
        "cancelOrder",
        "reqGlobalCancel",
        "exerciseOptions",
        "reqAccountSummary",
        "reqAccountSummaryAsync",
        "reqAccountUpdates",
        "reqAccountUpdatesMulti",
        "reqPositions",
        "reqPositionsMulti",
        "reqPnL",
        "reqPnLSingle",
        "reqExecutions",
        "reqOpenOrders",
        "reqAllOpenOrders",
        "reqCompletedOrders",
        # ib_async helper:
        "buy",
        "sell",
    }
)


def _python_files():
    return sorted(_SRC.rglob("*.py"))


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_order_or_account_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        # foo.bar() — attribute access in calls
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN:
                violations.append(f"{path.name}:{node.lineno} -> .{func.attr}()")
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN:
                violations.append(f"{path.name}:{node.lineno} -> {func.id}()")
        # Also catch bare attribute mentions (e.g. assigning the method) — paranoid.
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN:
            violations.append(f"{path.name}:{node.lineno} attr ref .{node.attr}")
    assert not violations, "Read-only invariant violated:\n  " + "\n  ".join(violations)
