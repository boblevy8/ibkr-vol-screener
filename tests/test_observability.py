"""Unit tests for observability helpers (no IBKR connection needed)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from ibkr_vol_screener.observability import (
    SubscriptionErrorTracker,
    configure_file_logging,
    detach_handler,
    is_scanner_cancel_receipt,
)


def test_is_scanner_cancel_receipt_matches_real_message():
    msg = "Error 162, reqId 5: Historical Market Data Service error message:API scanner subscription cancelled: 5"
    assert is_scanner_cancel_receipt(msg)
    assert is_scanner_cancel_receipt("API SCANNER SUBSCRIPTION CANCELLED")


def test_is_scanner_cancel_receipt_ignores_other_errors():
    assert not is_scanner_cancel_receipt("Error 162: pacing violation")
    assert not is_scanner_cancel_receipt("Error 10089: Market data not subscribed")
    assert not is_scanner_cancel_receipt(None)
    assert not is_scanner_cancel_receipt("")


def test_configure_file_logging_writes_records(tmp_path: Path):
    log_path = tmp_path / "run.log"
    handler = configure_file_logging(log_path, level=logging.DEBUG)
    try:
        logging.getLogger("test.observability").info("hello world")
        handler.flush()
        text = log_path.read_text(encoding="utf-8")
        assert "hello world" in text
        assert "INFO" in text
    finally:
        detach_handler(handler)


def test_configure_file_logging_rotation(tmp_path: Path):
    log_path = tmp_path / "rot.log"
    handler = configure_file_logging(
        log_path, level=logging.DEBUG, max_bytes=200, backups=2
    )
    try:
        lg = logging.getLogger("test.rot")
        for i in range(100):
            lg.info("padding-message-line-%03d", i)
        handler.flush()
    finally:
        detach_handler(handler)
    # Rotation creates rot.log + rot.log.1 (at least one backup).
    assert log_path.exists()
    backups = list(tmp_path.glob("rot.log.*"))
    assert backups, "expected at least one rotated backup file"


# --- SubscriptionErrorTracker ---


@dataclass
class _FakeEvent:
    callbacks: list

    def __iadd__(self, cb):
        self.callbacks.append(cb)
        return self

    def __isub__(self, cb):
        if cb in self.callbacks:
            self.callbacks.remove(cb)
        return self


class _FakeIB:
    def __init__(self):
        self.errorEvent = _FakeEvent(callbacks=[])


class _FakeContract:
    def __init__(self, symbol):
        self.symbol = symbol


def test_tracker_captures_subscription_codes_only():
    ib = _FakeIB()
    t = SubscriptionErrorTracker.attach(ib)
    # Subscription error -> captured.
    ib.errorEvent.callbacks[0](42, 10089, "Market data not subscribed.", _FakeContract("AAPL"))
    # Pacing error -> ignored.
    ib.errorEvent.callbacks[0](43, 162, "Pacing violation", _FakeContract("MSFT"))
    # Delayed-available -> captured.
    ib.errorEvent.callbacks[0](44, 10168, "Delayed data available", _FakeContract("WEED"))
    # Same symbol twice -> dedupe via unique_symbols.
    ib.errorEvent.callbacks[0](45, 10089, "Same again", _FakeContract("AAPL"))

    assert len(t.misses) == 3
    assert t.unique_symbols == ["AAPL", "WEED"]
    summary = t.summary()
    assert summary is not None
    assert "2 symbol" in summary
    assert "AAPL" in summary and "WEED" in summary


def test_tracker_summary_empty():
    ib = _FakeIB()
    t = SubscriptionErrorTracker.attach(ib)
    assert t.summary() is None
    t.detach(ib)
    assert ib.errorEvent.callbacks == []


def test_tracker_handles_missing_symbol():
    ib = _FakeIB()
    t = SubscriptionErrorTracker.attach(ib)
    ib.errorEvent.callbacks[0](42, 10089, "no contract", None)
    assert len(t.misses) == 1
    assert t.misses[0].symbol == ""
    assert t.unique_symbols == []
    assert t.summary() is None  # no usable symbols -> no summary


@pytest.mark.parametrize("bogus_code", [None, "not-an-int", float("nan")])
def test_tracker_ignores_non_integer_codes(bogus_code):
    ib = _FakeIB()
    t = SubscriptionErrorTracker.attach(ib)
    ib.errorEvent.callbacks[0](1, bogus_code, "msg", _FakeContract("X"))
    assert t.misses == []
