"""Read-only IBKR TWS volatility screener.

This package connects to TWS / IB Gateway and ranks stocks by their realized
1-minute volatility over the last 60 minutes across three markets:
US-major, Canada/TSX, and US OTC / pink sheets.

By design, this package never imports or calls any order-placement endpoint.
See tests/test_readonly.py for the AST-level invariant check.
"""

__version__ = "0.4.0"
