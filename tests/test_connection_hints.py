"""Tests for the friendlier connection-error hint formatter."""

from __future__ import annotations

from ibkr_vol_screener.ib_client import format_connect_hint


def test_connection_refused_hint():
    hint = format_connect_hint(
        ConnectionRefusedError("refused"), "127.0.0.1", 7497, 42
    )
    assert "not listening" in hint
    assert "paper TWS" in hint
    assert "127.0.0.1:7497" in hint
    # Mentions other ports so user can correct port choice.
    assert "7496" in hint
    assert "4002" in hint


def test_windows_winerror_10061_treated_as_refused():
    err = OSError("denied")
    err.winerror = 10061  # type: ignore[attr-defined]
    hint = format_connect_hint(err, "127.0.0.1", 4001, 42)
    assert "not listening" in hint
    assert "live IB Gateway" in hint


def test_timeout_hint():
    hint = format_connect_hint(TimeoutError(), "127.0.0.1", 7497, 42)
    assert "timed out" in hint
    assert "Trusted IPs" in hint


def test_client_id_collision_hint():
    hint = format_connect_hint(
        RuntimeError("clientId 42 already in use"), "127.0.0.1", 7497, 42
    )
    assert "clientId=42" in hint
    assert "--client-id 43" in hint


def test_handshake_failure_hint():
    hint = format_connect_hint(
        RuntimeError("API connection failed"), "127.0.0.1", 7497, 42
    )
    assert "Handshake" in hint
    assert "7497" in hint


def test_unknown_error_falls_back_with_diagnostic():
    hint = format_connect_hint(
        ValueError("something weird"), "host.example", 9999, 1
    )
    assert "host.example:9999" in hint
    assert "ValueError" in hint
    assert "something weird" in hint
