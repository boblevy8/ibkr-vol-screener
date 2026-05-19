"""Async connection wrapper for ib_async.

Exposes a single async context manager `ib_session()` that connects to TWS or
IB Gateway, yields the live `IB` instance, and guarantees disconnection on
exit (including on error).

This module is intentionally tiny. All higher-level logic (scanners, historical
bars, metrics) lives in sibling modules so it can be unit-tested without an
IB connection.

Read-only invariant: this package never calls placeOrder, cancelOrder,
reqAccountSummary, reqPositions, reqAccountUpdates, or reqPnL. See
tests/test_readonly.py for the AST-level check.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from ib_async import IB

log = logging.getLogger(__name__)


# Known port aliases used in the connection hint messaging.
_PORT_HINTS = {
    7497: "paper TWS",
    7496: "live TWS",
    4002: "paper IB Gateway",
    4001: "live IB Gateway",
}


def format_connect_hint(exc: BaseException, host: str, port: int, client_id: int) -> str:
    """Map a low-level connection exception to a one-line fix hint.

    Pure function — fully unit-testable by passing constructed exceptions.
    Returns a single string ending without a trailing newline.
    """
    port_label = _PORT_HINTS.get(port, f"port {port}")
    other_ports = ", ".join(
        f"{p} ({_PORT_HINTS[p]})" for p in _PORT_HINTS if p != port
    )

    # ConnectionRefusedError (and Windows' WinError 10061) -> not listening.
    win_refused = isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10061
    if isinstance(exc, ConnectionRefusedError) or win_refused:
        return (
            f"TWS / IB Gateway is not listening on {host}:{port} ({port_label}). "
            f"Is it running and logged in? Other common ports: {other_ports}. "
            f"In TWS, open File > Global Configuration > API > Settings and "
            f"tick 'Enable ActiveX and Socket Clients'."
        )

    # asyncio.TimeoutError -> reachable but no handshake.
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return (
            f"Connect to {host}:{port} timed out. Make sure {host} is in TWS API "
            f"'Trusted IPs' (Global Configuration > API > Settings > Trusted IPs)."
        )

    msg = str(exc).lower()
    # ib_async surfaces handshake / clientId problems as ConnectionError or RuntimeError
    # with text like "API connection failed" or "clientId X already in use".
    if "clientid" in msg or "client id" in msg or "already in use" in msg:
        return (
            f"clientId={client_id} is already in use on {host}:{port}. "
            f"Try --client-id {client_id + 1} or close the other API session."
        )
    if "api connection failed" in msg or "tws is not connected" in msg:
        return (
            f"Handshake to {host}:{port} ({port_label}) failed. "
            f"Are you on the right port? Paper TWS is 7497, live TWS is 7496, "
            f"paper Gateway is 4002, live Gateway is 4001."
        )

    # Fallback: keep the original error visible but tag it.
    return (
        f"Connection error talking to {host}:{port}: {type(exc).__name__}: {exc}. "
        f"Verify TWS is logged in, API is enabled, and the port matches "
        f"({port_label})."
    )


@contextlib.asynccontextmanager
async def ib_session(
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 42,
    *,
    timeout: float = 10.0,
    read_only_hint: bool = True,
) -> AsyncIterator[IB]:
    """Connect to TWS / IB Gateway and yield the IB handle.

    The `read_only_hint` argument is purely informational — it logs a reminder
    that the TWS-side "Read-Only API" checkbox should be enabled. The actual
    read-only guarantee comes from this package never invoking order/account
    endpoints (enforced by tests/test_readonly.py).
    """
    ib = IB()
    log.info("Connecting to IBKR at %s:%d (clientId=%d)", host, port, client_id)
    try:
        await ib.connectAsync(host=host, port=port, clientId=client_id, timeout=timeout)
    except Exception as exc:
        hint = format_connect_hint(exc, host, port, client_id)
        log.error(hint)
        raise
    log.info(
        "Connected. Server version=%s. Read-only hint: enable TWS API Settings -> "
        "'Read-Only API' for defense in depth.",
        ib.client.serverVersion() if ib.isConnected() else "?",
    )
    if not read_only_hint:
        log.debug("read_only_hint disabled by caller")
    try:
        yield ib
    finally:
        with contextlib.suppress(Exception):
            ib.disconnect()
        log.info("Disconnected from IBKR.")


def set_market_data_type(ib: IB, mode: int) -> None:
    """Switch market-data subscription mode.

    Modes: 1 = real-time, 2 = frozen, 3 = delayed, 4 = delayed-frozen.
    Useful when the account lacks live subscriptions for some venues.
    """
    if mode not in (1, 2, 3, 4):
        raise ValueError(f"market_data_type must be in 1..4, got {mode}")
    log.info("Setting market data type to %d (1=live, 3=delayed)", mode)
    ib.reqMarketDataType(mode)
