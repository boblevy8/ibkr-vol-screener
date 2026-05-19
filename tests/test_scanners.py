"""Unit tests for scanners.gather_candidates dedupe + skip behavior.

Uses a fake `IB` object that returns pre-canned ScanData rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from ibkr_vol_screener.config import MarketBucket, default_profile
from ibkr_vol_screener.scanners import gather_candidates


@dataclass
class _Contract:
    conId: int
    symbol: str
    exchange: str = "SMART"
    primaryExchange: str = "NASDAQ"
    currency: str = "USD"


@dataclass
class _CD:
    contract: _Contract


@dataclass
class _Row:
    contractDetails: _CD


class FakeIB:
    def __init__(self, rows_by_scan: dict[str, list[_Row]]):
        self._rows = rows_by_scan
        self.calls = []

    async def reqScannerDataAsync(self, sub, opts, filters):  # noqa: D401
        self.calls.append((sub.locationCode, sub.scanCode))
        return self._rows.get(sub.scanCode, [])


@pytest.fixture
def xml_text():
    p = Path(__file__).parent / "fixtures" / "scanner_params_sample.xml"
    return p.read_text(encoding="utf-8")


def _row(con_id: int, sym: str) -> _Row:
    return _Row(contractDetails=_CD(contract=_Contract(conId=con_id, symbol=sym)))


@pytest.mark.asyncio
async def test_gather_dedupes_and_unions_source_codes(xml_text):
    rows = {
        "HOT_BY_VOLUME": [_row(1, "AAA"), _row(2, "BBB")],
        "TOP_PERC_GAIN": [_row(1, "AAA"), _row(3, "CCC")],
        "TOP_PERC_LOSE": [],
        "MOST_ACTIVE": [_row(2, "BBB")],
        "TOP_TRADE_COUNT": [],  # not in XML for this fixture -> skipped silently
    }
    ib = FakeIB(rows)
    profile = default_profile(MarketBucket.US_MAJOR)
    out = await gather_candidates(
        ib, profile, scanner_xml=xml_text, top_n_per_scan=10, max_candidates=100
    )
    by_id = {c.con_id: c for c in out}
    assert set(by_id) == {1, 2, 3}
    assert by_id[1].source_scan_codes == {"HOT_BY_VOLUME", "TOP_PERC_GAIN"}
    assert by_id[2].source_scan_codes == {"HOT_BY_VOLUME", "MOST_ACTIVE"}
    assert by_id[3].source_scan_codes == {"TOP_PERC_GAIN"}


@pytest.mark.asyncio
async def test_gather_skips_unknown_location(xml_text):
    ib = FakeIB({})
    profile = default_profile(MarketBucket.OTC)
    # Force a bogus location not in XML.
    from dataclasses import replace

    profile = replace(profile, location_code="STK.BOGUS.LOC")
    out = await gather_candidates(
        ib, profile, scanner_xml=xml_text, top_n_per_scan=10, max_candidates=100
    )
    assert out == []
    assert ib.calls == []  # nothing was issued


@pytest.mark.asyncio
async def test_gather_respects_max_candidates(xml_text):
    rows = {
        "HOT_BY_VOLUME": [_row(i, f"S{i}") for i in range(1, 21)],
        "TOP_PERC_GAIN": [_row(i, f"S{i}") for i in range(15, 35)],
        "TOP_PERC_LOSE": [],
        "MOST_ACTIVE": [],
    }
    ib = FakeIB(rows)
    profile = default_profile(MarketBucket.US_MAJOR)
    out = await gather_candidates(
        ib, profile, scanner_xml=xml_text, top_n_per_scan=20, max_candidates=10
    )
    assert len(out) == 10
    # The dedupe overlap (ids 15-20) hit multiple scan codes; the trim
    # prioritises multi-scan candidates first.
    multi = [c for c in out if len(c.source_scan_codes) > 1]
    assert multi, "candidates with multiple scan codes should appear first"
