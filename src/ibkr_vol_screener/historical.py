"""Bounded-concurrent 1-minute historical bar fetcher.

Implements pacing-aware retrieval of `durationStr=<N>S` of 1-minute bars per
candidate, with:
  * `asyncio.Semaphore` to cap concurrent open historical requests
  * minimum inter-request sleep to spread load
  * exponential backoff retry on transient errors (IBKR codes 162 / 165 / 366)
  * OTC fallback: if TRADES returns 0 bars, retry with whatToShow=MIDPOINT
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .config import MarketBucket
from .scanners import Candidate

if TYPE_CHECKING:
    from ib_async import IB

log = logging.getLogger(__name__)

# IBKR error codes worth retrying. 162 covers most pacing / "historical data
# service" failures; 165 = "no data permission"; 366 = "no historical data
# query found" (transient on busy gateways).
_RETRYABLE_ERROR_CODES = frozenset({162, 366})


@dataclass
class BarsResult:
    candidate: Candidate
    bars: list[Any] = field(default_factory=list)
    error: str | None = None
    what_to_show: str = "TRADES"


def _is_otc(cand: Candidate) -> bool:
    return cand.bucket is MarketBucket.OTC


def _build_contract(cand: Candidate) -> Any:
    """Construct an ib_async Contract from a Candidate.

    Prefer the conId path (skips re-qualification round-trip). Fall back to
    symbol+primaryExchange when conId is missing.
    """
    from ib_async import Contract, Stock

    if cand.con_id:
        return Contract(
            conId=cand.con_id,
            secType="STK",
            exchange="SMART",
            currency=cand.currency or "USD",
        )
    return Stock(
        cand.symbol,
        exchange="SMART",
        primaryExchange=cand.primary_exchange or "",
        currency=cand.currency or "USD",
    )


async def _request_bars(
    ib: IB,
    contract: Any,
    *,
    duration_s: int,
    bar_size: str,
    what_to_show: str,
    use_rth: bool,
) -> list[Any]:
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr=f"{duration_s} S",
        barSizeSetting=bar_size,
        whatToShow=what_to_show,
        useRTH=use_rth,
        formatDate=2,  # epoch seconds
    )
    return list(bars or [])


def _classify_error(exc: BaseException) -> tuple[int | None, bool]:
    """Return (error_code, is_retryable) for an ib_async historical-data error."""
    code = getattr(exc, "errorCode", None) or getattr(exc, "code", None)
    if code is None:
        text = str(exc)
        for k in _RETRYABLE_ERROR_CODES:
            if f" {k} " in f" {text} " or f"{k}," in text:
                return k, True
        return None, False
    try:
        code_int = int(code)
    except Exception:
        return None, False
    return code_int, code_int in _RETRYABLE_ERROR_CODES


async def fetch_bars(
    ib: IB,
    candidate: Candidate,
    *,
    duration_s: int = 5400,
    bar_size: str = "1 min",
    what_to_show: str = "TRADES",
    use_rth: bool = False,
    max_retries: int = 3,
    request_delay_s: float = 0.25,
) -> BarsResult:
    """Fetch bars for one candidate with retries + OTC fallback."""
    contract = _build_contract(candidate)
    attempt = 0
    last_err: str | None = None
    while attempt <= max_retries:
        try:
            if request_delay_s > 0:
                await asyncio.sleep(request_delay_s)
            bars = await _request_bars(
                ib,
                contract,
                duration_s=duration_s,
                bar_size=bar_size,
                what_to_show=what_to_show,
                use_rth=use_rth,
            )
            if bars:
                return BarsResult(candidate=candidate, bars=bars, what_to_show=what_to_show)
            # Empty result: for OTC, try MIDPOINT once.
            if _is_otc(candidate) and what_to_show == "TRADES":
                log.info(
                    "No TRADES bars for OTC %s; falling back to MIDPOINT.", candidate.symbol
                )
                what_to_show = "MIDPOINT"
                attempt = 0  # reset retries for fallback
                continue
            return BarsResult(
                candidate=candidate,
                bars=[],
                error="no_bars",
                what_to_show=what_to_show,
            )
        except Exception as exc:
            code, retryable = _classify_error(exc)
            last_err = f"{type(exc).__name__}: {exc}"
            if not retryable or attempt >= max_retries:
                log.warning(
                    "Historical fetch failed for %s (code=%s, attempt=%d): %s",
                    candidate.symbol,
                    code,
                    attempt,
                    last_err,
                )
                return BarsResult(
                    candidate=candidate,
                    bars=[],
                    error=last_err,
                    what_to_show=what_to_show,
                )
            backoff = (2**attempt) + random.uniform(0, 0.5)
            log.info(
                "Pacing/transient error for %s (code=%s); retrying in %.1fs (attempt %d/%d)",
                candidate.symbol,
                code,
                backoff,
                attempt + 1,
                max_retries,
            )
            await asyncio.sleep(backoff)
            attempt += 1
    return BarsResult(
        candidate=candidate,
        bars=[],
        error=last_err or "max_retries_exceeded",
        what_to_show=what_to_show,
    )


async def fetch_all(
    ib: IB,
    candidates: list[Candidate],
    *,
    concurrency: int = 4,
    duration_s: int = 5400,
    bar_size: str = "1 min",
    what_to_show: str = "TRADES",
    use_rth: bool = False,
    max_retries: int = 3,
    request_delay_s: float = 0.25,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[BarsResult]:
    """Fetch bars for all candidates with bounded concurrency.

    `progress_callback(completed, total)` is invoked once per finished candidate
    (in completion order). Failures count as completed.
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    total = len(candidates)
    log.info(
        "Fetching %d historical bar series (concurrency=%d, duration=%ds, useRTH=%s)",
        total,
        concurrency,
        duration_s,
        use_rth,
    )

    async def worker(cand: Candidate) -> BarsResult:
        async with sem:
            return await fetch_bars(
                ib,
                cand,
                duration_s=duration_s,
                bar_size=bar_size,
                what_to_show=what_to_show,
                use_rth=use_rth,
                max_retries=max_retries,
                request_delay_s=request_delay_s,
            )

    tasks = [asyncio.create_task(worker(c)) for c in candidates]
    results: list[BarsResult] = []
    done = 0
    for fut in asyncio.as_completed(tasks):
        try:
            results.append(await fut)
        except Exception as exc:
            log.error("Worker crashed: %s", exc)
        done += 1
        if progress_callback is not None:
            try:
                progress_callback(done, total)
            except Exception as exc:
                log.debug("progress_callback raised: %s", exc)
    ok = sum(1 for r in results if r.bars)
    log.info("Bars fetched: %d / %d with data.", ok, total)
    return results


async def fetch_prior_closes(
    ib: IB,
    candidates: list[Candidate],
    *,
    concurrency: int = 4,
    max_retries: int = 2,
    request_delay_s: float = 0.25,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[int, float]:
    """Return {conId: prior_session_close} for each candidate that resolves.

    Issues one daily-bar request per candidate (`durationStr="3 D"`,
    `barSizeSetting="1 day"`, `whatToShow="TRADES"`, `useRTH=True`) and takes
    the close of the bar before today. Candidates that fail are simply absent
    from the returned dict.

    This doubles pacing cost vs the regular `fetch_all`, so the CLI gates it
    behind `--with-gap`.
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    out: dict[int, float] = {}
    total = len(candidates)
    log.info("Fetching prior-session closes for %d candidates", total)

    async def worker(cand: Candidate) -> None:
        contract = _build_contract(cand)
        attempt = 0
        while attempt <= max_retries:
            try:
                async with sem:
                    if request_delay_s > 0:
                        await asyncio.sleep(request_delay_s)
                    bars = await ib.reqHistoricalDataAsync(
                        contract,
                        endDateTime="",
                        durationStr="3 D",
                        barSizeSetting="1 day",
                        whatToShow="TRADES",
                        useRTH=True,
                        formatDate=2,
                    )
                bars = list(bars or [])
                # The last bar is today's incomplete bar; the second-to-last
                # is the previous session close. If only one bar returned,
                # treat it as the prior close.
                if len(bars) >= 2:
                    out[cand.con_id] = float(bars[-2].close)
                elif bars:
                    out[cand.con_id] = float(bars[-1].close)
                return
            except Exception as exc:
                code, retryable = _classify_error(exc)
                if not retryable or attempt >= max_retries:
                    log.debug("Prior-close fetch failed for %s (code=%s): %s",
                              cand.symbol, code, exc)
                    return
                await asyncio.sleep((2**attempt) + random.uniform(0, 0.5))
                attempt += 1

    tasks = [asyncio.create_task(worker(c)) for c in candidates]
    done = 0
    for fut in asyncio.as_completed(tasks):
        try:
            await fut
        except Exception as exc:
            log.error("Prior-close worker crashed: %s", exc)
        done += 1
        if progress_callback is not None:
            try:
                progress_callback(done, total)
            except Exception as exc:
                log.debug("progress_callback raised: %s", exc)
    log.info("Prior closes resolved for %d / %d candidates.", len(out), total)
    return out
