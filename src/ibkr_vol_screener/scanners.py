"""Candidate generation via IBKR market scanners.

For each market bucket we run several scan codes (HOT_BY_VOLUME, TOP_PERC_GAIN,
TOP_PERC_LOSE, MOST_ACTIVE, ...) and merge their results into a deduplicated
list of `Candidate`s keyed by `conId`. Missing or failing scan codes are
logged and skipped — we never hard-fail a whole bucket because one code is
unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import MarketBucket, MarketProfile
from .scanner_params import has_location, has_scan_code

if TYPE_CHECKING:
    from ib_async import IB

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    con_id: int
    symbol: str
    exchange: str
    primary_exchange: str
    currency: str
    bucket: MarketBucket
    source_scan_codes: set[str] = field(default_factory=set)

    def merge(self, other: Candidate) -> None:
        self.source_scan_codes |= other.source_scan_codes


def _candidate_from_scan_row(row: object, bucket: MarketBucket, scan_code: str) -> Candidate | None:
    """Convert an ib_async ScanData row into a Candidate.

    Returns None when the row lacks a contract (defensive — shouldn't happen).
    """
    cd = getattr(row, "contractDetails", None)
    if cd is None:
        return None
    c = getattr(cd, "contract", None)
    if c is None or not getattr(c, "conId", 0):
        return None
    return Candidate(
        con_id=int(c.conId),
        symbol=str(getattr(c, "symbol", "") or ""),
        exchange=str(getattr(c, "exchange", "") or "SMART"),
        primary_exchange=str(getattr(c, "primaryExchange", "") or ""),
        currency=str(getattr(c, "currency", "") or ""),
        bucket=bucket,
        source_scan_codes={scan_code},
    )


async def run_scan(
    ib: IB,
    *,
    location_code: str,
    scan_code: str,
    bucket: MarketBucket,
    top_n: int,
    instrument: str = "STK",
    min_price: float | None = None,
    max_price: float | None = None,
) -> list[Candidate]:
    """Issue one scanner request, return a deduped Candidate list."""
    from ib_async import ScannerSubscription, TagValue

    sub = ScannerSubscription(
        instrument=instrument,
        locationCode=location_code,
        scanCode=scan_code,
        numberOfRows=top_n,
    )
    filters: list[TagValue] = []
    if min_price is not None:
        filters.append(TagValue("priceAbove", str(min_price)))
    if max_price is not None:
        filters.append(TagValue("priceBelow", str(max_price)))

    try:
        rows = await ib.reqScannerDataAsync(sub, [], filters)
    except Exception as exc:
        log.warning(
            "Scanner request failed: location=%s scan=%s err=%s",
            location_code,
            scan_code,
            exc,
        )
        return []

    out: list[Candidate] = []
    for row in rows or []:
        cand = _candidate_from_scan_row(row, bucket, scan_code)
        if cand is not None:
            out.append(cand)
    log.info(
        "Scan %s @ %s -> %d candidates",
        scan_code,
        location_code,
        len(out),
    )
    return out


async def gather_candidates(
    ib: IB,
    profile: MarketProfile,
    *,
    scanner_xml: str | None,
    top_n_per_scan: int,
    max_candidates: int,
) -> list[Candidate]:
    """Run all configured scan codes for a market and merge by conId.

    Skips scan codes / locations that are not present in the scanner XML
    (when XML is provided) with a warning.
    """
    if profile.location_code is None:
        log.warning("No location_code for bucket %s; skipping.", profile.bucket.value)
        return []

    if scanner_xml is not None and not has_location(scanner_xml, profile.location_code):
        log.warning(
            "Location %s not in scanner XML; skipping bucket %s.",
            profile.location_code,
            profile.bucket.value,
        )
        return []

    merged: dict[int, Candidate] = {}
    for scan_code in profile.scan_codes:
        if scanner_xml is not None and not has_scan_code(scanner_xml, scan_code):
            log.warning(
                "Scan code %s not available; skipping (bucket=%s).",
                scan_code,
                profile.bucket.value,
            )
            continue
        cands = await run_scan(
            ib,
            location_code=profile.location_code,
            scan_code=scan_code,
            bucket=profile.bucket,
            top_n=top_n_per_scan,
            min_price=profile.min_price if profile.min_price > 0 else None,
            max_price=profile.max_price,
        )
        for c in cands:
            if c.con_id in merged:
                merged[c.con_id].merge(c)
            else:
                merged[c.con_id] = c

    out = list(merged.values())
    if len(out) > max_candidates:
        log.info(
            "Bucket %s: trimming candidates %d -> %d to respect pacing budget.",
            profile.bucket.value,
            len(out),
            max_candidates,
        )
        # Prioritize candidates that hit multiple scan codes (more "interesting").
        out.sort(key=lambda c: (-len(c.source_scan_codes), c.symbol))
        out = out[:max_candidates]
    log.info("Bucket %s: %d unique candidates", profile.bucket.value, len(out))
    return out
