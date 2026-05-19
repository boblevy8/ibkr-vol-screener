"""User-supplied watchlist support.

Reads a one-symbol-per-line file (lines starting with `#` and blank lines
are ignored), qualifies each symbol via `reqContractDetailsAsync` to
resolve conId / primaryExchange / currency, and emits `Candidate`
objects tagged with `source_scan_codes={"WATCHLIST"}` so they merge
cleanly with the IBKR scanner output.

`reqContractDetailsAsync` is a read-only endpoint. The AST scan in
`tests/test_readonly.py` does not forbid it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .config import MarketBucket
from .scanners import Candidate

if TYPE_CHECKING:
    from ib_async import IB

log = logging.getLogger(__name__)


def load_watchlist(path: Path) -> list[str]:
    """Parse a watchlist file into a deterministic list of upper-cased symbols.

    Format: one symbol per line. Lines starting with `#` are comments.
    Blank lines are ignored. Surrounding whitespace is stripped. Duplicate
    symbols are removed, preserving first-occurrence order.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Watchlist file not found: {path}")
    seen: set[str] = set()
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sym = line.upper()
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    log.info("Loaded watchlist: %d symbols from %s", len(out), path)
    return out


async def qualify_watchlist(
    ib: IB,
    symbols: list[str],
    *,
    default_bucket: MarketBucket = MarketBucket.US_MAJOR,
    default_currency: str = "USD",
) -> list[Candidate]:
    """Resolve each symbol via `reqContractDetailsAsync(Stock(...))`.

    Returns a list of Candidate objects in the same order as the input.
    Symbols that fail to resolve are logged and skipped.
    """
    from ib_async import Stock

    out: list[Candidate] = []
    for sym in symbols:
        stock = Stock(sym, exchange="SMART", currency=default_currency)
        try:
            details = await ib.reqContractDetailsAsync(stock)
        except Exception as exc:
            log.warning("Watchlist resolve failed for %s: %s", sym, exc)
            continue
        if not details:
            log.warning("Watchlist symbol not resolved: %s", sym)
            continue
        cd = details[0]
        c = cd.contract
        out.append(
            Candidate(
                con_id=int(c.conId),
                symbol=str(c.symbol or sym),
                exchange=str(c.exchange or "SMART"),
                primary_exchange=str(getattr(c, "primaryExchange", "") or ""),
                currency=str(c.currency or default_currency),
                bucket=default_bucket,
                source_scan_codes={"WATCHLIST"},
            )
        )
    log.info("Qualified %d / %d watchlist symbols", len(out), len(symbols))
    return out
