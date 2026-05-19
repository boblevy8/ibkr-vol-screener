"""Tests for the watchlist loader and qualifier."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from ibkr_vol_screener.config import MarketBucket
from ibkr_vol_screener.watchlist import load_watchlist, qualify_watchlist


def test_load_watchlist_ignores_comments_and_blanks(tmp_path: Path):
    p = tmp_path / "tickers.txt"
    p.write_text("AAPL\n# a comment\n\nMSFT\n  nvda  \n# another\nAAPL\n", encoding="utf-8")
    out = load_watchlist(p)
    assert out == ["AAPL", "MSFT", "NVDA"]


def test_load_watchlist_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_watchlist(tmp_path / "missing.txt")


# --- qualify_watchlist (mocked IB) ---


@dataclass
class _Contract:
    conId: int
    symbol: str
    exchange: str = "SMART"
    primaryExchange: str = "NASDAQ"
    currency: str = "USD"


@dataclass
class _ContractDetails:
    contract: _Contract


class _FakeIB:
    def __init__(self, mapping: dict[str, _Contract] | None = None):
        self._map = mapping or {}
        self.calls: list[str] = []

    async def reqContractDetailsAsync(self, stock):
        self.calls.append(stock.symbol)
        c = self._map.get(stock.symbol)
        if c is None:
            return []
        return [_ContractDetails(contract=c)]


@pytest.mark.asyncio
async def test_qualify_watchlist_resolves_known_symbols():
    ib = _FakeIB(
        mapping={
            "AAPL": _Contract(conId=265598, symbol="AAPL"),
            "MSFT": _Contract(conId=272093, symbol="MSFT"),
        }
    )
    out = await qualify_watchlist(ib, ["AAPL", "MSFT", "UNKNOWN"])
    assert len(out) == 2
    assert {c.symbol for c in out} == {"AAPL", "MSFT"}
    assert all(c.source_scan_codes == {"WATCHLIST"} for c in out)
    assert all(c.bucket is MarketBucket.US_MAJOR for c in out)
    # All three were queried, even the one that failed.
    assert ib.calls == ["AAPL", "MSFT", "UNKNOWN"]


@pytest.mark.asyncio
async def test_qualify_watchlist_handles_exception():
    class _Boom(_FakeIB):
        async def reqContractDetailsAsync(self, stock):
            self.calls.append(stock.symbol)
            if stock.symbol == "EXPLODE":
                raise RuntimeError("network glitch")
            return [_ContractDetails(_Contract(conId=1, symbol=stock.symbol))]

    ib = _Boom()
    out = await qualify_watchlist(ib, ["AAA", "EXPLODE", "BBB"])
    # AAA and BBB resolved; EXPLODE skipped.
    assert {c.symbol for c in out} == {"AAA", "BBB"}
