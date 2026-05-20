"""ATR-stop trade planning + fixed-fractional position sizing.

This is the math layer behind the `plan` CLI command. Given a
`ScreenRow` (which already has `atr_pct_60m`, `last_close`, etc.)
and the user's account-value + risk preference, compute a complete
trade plan: entry, stop, target, position size, R-multiple.

Read-only by construction: no IB endpoints, no order placement.
The CLI command that calls `compute_plan(...)` uses
`qualifyContractsAsync` + `reqHistoricalDataAsync` only — both
already in the allow-list.
"""

from __future__ import annotations

from dataclasses import dataclass

from .metrics import ScreenRow


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    side: str
    last_close: float
    atr_pct_60m: float
    range_pct: float

    entry: float
    stop: float
    target: float
    stop_distance: float
    target_distance: float

    risk_dollar: float
    position_size: int
    notional: float
    notional_pct_of_account: float

    r_multiple: float

    # User-supplied knobs (carried through for the rendered output).
    account_value: float
    risk_pct: float
    atr_multiple: float
    target_r: float


def compute_plan(
    row: ScreenRow,
    *,
    account_value: float,
    risk_pct: float = 1.0,
    side: str = "long",
    atr_multiple: float = 1.5,
    target_r: float = 2.0,
) -> TradePlan:
    """ATR-stop fixed-fractional trade plan.

    * `stop_distance = atr_multiple * (atr_pct_60m/100 * last_close)`
    * `risk_dollar = account_value * risk_pct / 100`
    * `position_size = floor(risk_dollar / stop_distance)` shares
    * target placed at `target_r * stop_distance` in the trade's
      direction (away from entry; long = above, short = below).

    Returns a fully populated `TradePlan`. Raises `ValueError` on
    degenerate inputs (zero ATR, bad side, risk_pct out of range,
    non-positive account_value).
    """
    if side not in {"long", "short"}:
        raise ValueError(f"side must be 'long' or 'short' (got {side!r})")
    if not (0 < risk_pct <= 100):
        raise ValueError(f"risk_pct must be in (0, 100] (got {risk_pct})")
    if account_value <= 0:
        raise ValueError(f"account_value must be > 0 (got {account_value})")
    if row.atr_pct_60m <= 0:
        raise ValueError(
            "ATR is zero or negative; cannot size a trade with a meaningless stop"
        )
    if row.last_close <= 0:
        raise ValueError(f"last_close must be > 0 (got {row.last_close})")

    entry = float(row.last_close)
    atr_dollar = (row.atr_pct_60m / 100.0) * entry
    stop_distance = atr_multiple * atr_dollar
    target_distance = target_r * stop_distance

    if side == "long":
        stop = entry - stop_distance
        target = entry + target_distance
    else:
        stop = entry + stop_distance
        target = entry - target_distance

    risk_dollar = account_value * (risk_pct / 100.0)
    position_size = int(risk_dollar // stop_distance) if stop_distance > 0 else 0
    notional = position_size * entry
    notional_pct = (notional / account_value * 100.0) if account_value > 0 else 0.0
    r_multiple = (target_distance / stop_distance) if stop_distance > 0 else 0.0

    return TradePlan(
        symbol=row.symbol,
        side=side,
        last_close=entry,
        atr_pct_60m=row.atr_pct_60m,
        range_pct=row.range_pct,
        entry=entry,
        stop=stop,
        target=target,
        stop_distance=stop_distance,
        target_distance=target_distance,
        risk_dollar=risk_dollar,
        position_size=position_size,
        notional=notional,
        notional_pct_of_account=notional_pct,
        r_multiple=r_multiple,
        account_value=account_value,
        risk_pct=risk_pct,
        atr_multiple=atr_multiple,
        target_r=target_r,
    )


def render_plan_panel(plan: TradePlan):
    """Build a Rich renderable for `plan SYMBOL` console output."""
    from rich.panel import Panel
    from rich.table import Table

    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_column("k", style="bold")
    t.add_column("v", justify="right")

    side_color = "green" if plan.side == "long" else "red"
    t.add_row("Symbol", f"[bold cyan]{plan.symbol}[/bold cyan]")
    t.add_row("Side", f"[{side_color}]{plan.side.upper()}[/{side_color}]")
    t.add_row("Last close", f"${plan.last_close:.2f}")
    t.add_row("ATR%(60m)", f"{plan.atr_pct_60m:.2f}%")
    t.add_row("Range%(60m)", f"{plan.range_pct:.2f}%")
    t.add_row("", "")
    t.add_row("Entry", f"[bold]${plan.entry:.2f}[/bold]")
    t.add_row(
        "Stop",
        f"[red]${plan.stop:.2f}[/red]   (-{plan.stop_distance:.2f})",
    )
    t.add_row(
        "Target",
        f"[green]${plan.target:.2f}[/green]   (+{plan.target_distance:.2f})",
    )
    t.add_row("R:R", f"[bold yellow]{plan.r_multiple:.2f}R[/bold yellow]")
    t.add_row("", "")
    t.add_row(
        "Risk",
        f"${plan.risk_dollar:,.2f}  ({plan.risk_pct:.2f}% of ${plan.account_value:,.0f})",
    )
    t.add_row(
        "Position",
        f"[bold]{plan.position_size:,} shares[/bold]   "
        f"(${plan.notional:,.0f}, {plan.notional_pct_of_account:.1f}% of acct)",
    )

    title = (
        f"Trade plan ({plan.atr_multiple:g}× ATR stop, {plan.target_r:g}R target)"
    )
    return Panel(t, title=title, border_style=side_color)
