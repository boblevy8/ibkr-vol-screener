"""Logging + IBKR-error observability helpers.

Two responsibilities, kept in one file because both are about *seeing what
happened* during a screen run:

* `configure_file_logging(path)` — attaches a `RotatingFileHandler` to the
  root logger so users can `--log-file ./run.log` and get a full DEBUG trail
  on disk even when console output is INFO-level.

* `SubscriptionErrorTracker` — subscribes to `ib.errorEvent` and records
  symbols that ran into a known "market data not subscribed" error code.
  At the end of a run, the CLI prints a one-line summary so users notice
  silent drops without scrolling through logs.

Both pieces are pure-Python and trivially unit-testable without an IB
connection.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# IBKR error codes for "data not subscribed" family.
# Sources: TWS API docs + observed runtime behavior on paper.
_SUBSCRIPTION_ERROR_CODES: frozenset[int] = frozenset(
    {
        354,    # "Requested market data is not subscribed."
        10089,  # Same family, newer wording.
        10090,  # "Part of requested market data is not subscribed."
        10091,
        10168,  # "Delayed market data is available."
    }
)

# Scanner-cancellation receipts are surfaced by ib_async as errorCode=162 with
# the text "API scanner subscription cancelled". They are normal completion
# events, not failures, so we filter them from the console error stream.
_SCANNER_CANCEL_RE = re.compile(r"scanner subscription cancelled", re.IGNORECASE)


def configure_file_logging(
    path: Path,
    *,
    level: int = logging.DEBUG,
    max_bytes: int = 10_000_000,
    backups: int = 5,
) -> logging.Handler:
    """Attach a RotatingFileHandler to the root logger.

    Returns the handler so callers can detach it on shutdown (the CLI does
    not, since the process is about to exit anyway, but tests need to detach
    to release the file).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    )
    root = logging.getLogger()
    # Make sure the root logger lets DEBUG records through to the file
    # even when console is INFO.
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    root.addHandler(handler)
    log.info("File logging enabled -> %s (level=%s)", path, logging.getLevelName(level))
    return handler


def detach_handler(handler: logging.Handler) -> None:
    """Remove + close a handler from the root logger. Used by tests."""
    root = logging.getLogger()
    if handler in root.handlers:
        root.removeHandler(handler)
    try:
        handler.close()
    except Exception:
        pass


def is_scanner_cancel_receipt(error_string: str | None) -> bool:
    """True for IBKR error code 162 "API scanner subscription cancelled" — a
    benign completion receipt, not an actionable failure.
    """
    if not error_string:
        return False
    return bool(_SCANNER_CANCEL_RE.search(error_string))


@dataclass
class SubscriptionMiss:
    """One observed (symbol, code, message) tuple."""

    symbol: str
    code: int
    message: str


@dataclass
class SubscriptionErrorTracker:
    """Listens to `IB.errorEvent` and records subscription-related drops.

    Usage:
        tracker = SubscriptionErrorTracker.attach(ib)
        ...
        summary = tracker.summary()
        tracker.detach(ib)
    """

    misses: list[SubscriptionMiss] = field(default_factory=list)
    _attached_ib: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def attach(cls, ib: Any) -> SubscriptionErrorTracker:
        tracker = cls(_attached_ib=ib)
        # ib_async's errorEvent signature is (reqId, errorCode, errorString, contract).
        ib.errorEvent += tracker._on_error
        return tracker

    def detach(self, ib: Any | None = None) -> None:
        target = ib or self._attached_ib
        if target is None:
            return
        try:
            target.errorEvent -= self._on_error
        except Exception:
            pass
        self._attached_ib = None

    def _on_error(self, reqId, errorCode, errorString, contract=None, *_, **__):  # noqa: N803,ARG002
        try:
            code = int(errorCode)
        except (TypeError, ValueError):
            return
        if code not in _SUBSCRIPTION_ERROR_CODES:
            return
        symbol = ""
        if contract is not None:
            symbol = str(getattr(contract, "symbol", "") or "")
        self.misses.append(
            SubscriptionMiss(symbol=symbol, code=code, message=str(errorString or ""))
        )

    @property
    def unique_symbols(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for m in self.misses:
            if m.symbol and m.symbol not in seen:
                seen.add(m.symbol)
                out.append(m.symbol)
        return out

    def summary(self, *, sample: int = 5) -> str | None:
        """Return a human-readable end-of-run line, or None if no misses."""
        syms = self.unique_symbols
        if not syms:
            return None
        head = ", ".join(syms[:sample])
        tail = f" (+{len(syms) - sample} more)" if len(syms) > sample else ""
        return (
            f"{len(syms)} symbol(s) dropped due to missing market-data subscription: "
            f"{head}{tail}. Try --allow-delayed, or subscribe to the relevant data "
            f"package in IBKR Client Portal."
        )
