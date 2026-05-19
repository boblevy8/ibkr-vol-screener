"""Integration test against a live TWS / IB Gateway instance.

Guarded by IBKR_LIVE_TEST=1 so normal CI / contributor runs never touch it.
Requires:
  * TWS or IB Gateway running on 127.0.0.1:7497 (paper).
  * "Enable ActiveX and Socket Clients" + "Read-Only API" checked.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("IBKR_LIVE_TEST") != "1",
    reason="Set IBKR_LIVE_TEST=1 to run integration tests against live TWS.",
)


def test_scanner_params_roundtrip():
    from ibkr_vol_screener.ib_client import ib_session
    from ibkr_vol_screener.scanner_params import fetch_scanner_xml, parse_locations

    async def _go():
        async with ib_session() as ib:
            xml = await fetch_scanner_xml(ib)
            assert xml, "Empty scanner XML"
            codes = {loc.code for loc in parse_locations(xml)}
            assert "STK.US.MAJOR" in codes
            assert "STK.US.MINOR" in codes

    asyncio.run(_go())
