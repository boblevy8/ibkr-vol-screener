"""Fetch + parse the IBKR scanner-parameters XML.

The XML returned by `IB.reqScannerParameters()` is the source of truth for:
  * Valid `instrument` codes
  * Valid `locationCode` strings (e.g. STK.US.MAJOR, STK.US.MINOR, and whatever
    TSX/Canada happens to be on this account)
  * Valid `scanCode` strings

We parse the XML with stdlib ElementTree (no external XML deps) and cache it
on disk so subsequent `once`/`watch` invocations don't refetch it every time.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ib_async import IB

log = logging.getLogger(__name__)


# Priority list for auto-discovering a TSX/Canada location code from XML.
# Confirmed against live scanner XML: STK.NA.CANADA, STK.NA.TSE, STK.NA.VENTURE.
_TSX_CANDIDATES_PRIORITY = (
    "STK.NA.CANADA",
    "STK.NA.TSE",
    "STK.NA.VENTURE",
)
_TSX_KEYWORDS = ("CANADA", "TSE", "TSX", "VENTURE", "TORONTO")

# Priority list for UK/EU location code discovery.
# IMPORTANT: IBKR's scanner is *not configured* for the broad "STK.EU"
# location even though that code appears in the XML (verified live
# 2026-05-19: code 365 + "Market Scanner is not configured for one of the
# chosen locations"). Country-specific codes are required. We default to
# LSE (UK, largest liquidity for English-speakers); users can override via
# `[profile.uk_eu] location_code = "STK.EU.IBIS"` etc.
_UK_EU_CANDIDATES_PRIORITY = (
    "STK.EU.LSE",
    "STK.EU.IBIS",
    "STK.EU.IBIS-XETRA",
    "STK.EU.AEB",
    "STK.EU.SBF",
    "STK.EU.BVME",
    "STK.EU.EBS",
    "STK.EU",  # last-resort catch-all; scanners typically rejects it.
)
_UK_EU_KEYWORDS = ("EU", "LSE", "LONDON", "FRANKFURT", "IBIS", "XETRA", "EUREX")


@dataclass(frozen=True)
class Location:
    code: str
    display_name: str
    instruments: tuple[str, ...]  # which instrument types support this location


@dataclass(frozen=True)
class ScanCode:
    code: str
    display_name: str
    instruments: tuple[str, ...]


async def fetch_scanner_xml(ib: IB) -> str:
    """Pull the full scanner-parameters XML blob from IBKR (async)."""
    log.info("Requesting scanner parameters XML from IBKR...")
    xml = await ib.reqScannerParametersAsync()
    log.info("Received scanner XML (%d chars).", len(xml or ""))
    return xml or ""


def cache_path(cache_dir: Path) -> Path:
    return cache_dir / "scanner_params.xml"


def write_cache(xml: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_path(cache_dir)
    p.write_text(xml, encoding="utf-8")
    log.debug("Wrote scanner XML cache to %s", p)
    return p


def read_cache(cache_dir: Path, max_age_h: float) -> str | None:
    p = cache_path(cache_dir)
    if not p.exists():
        return None
    age_h = (time.time() - p.stat().st_mtime) / 3600.0
    if age_h > max_age_h:
        log.debug("Scanner XML cache too old (%.1fh > %.1fh); refetching.", age_h, max_age_h)
        return None
    return p.read_text(encoding="utf-8")


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def parse_locations(xml: str) -> list[Location]:
    """Extract <Location> entries from the scanner XML.

    Each <Location> has a <locationCode>, <displayName>, and a list of
    <Instruments>/<Instrument> children naming the instrument types that
    support this location (we keep only those including STK).
    """
    root = _root(xml)
    out: list[Location] = []
    # The XML uses <LocationTree>/<Location> nodes; we walk all <Location>.
    for loc in root.iter("Location"):
        code_el = loc.find("locationCode")
        name_el = loc.find("displayName")
        if code_el is None or not code_el.text:
            continue
        code = code_el.text.strip()
        name = (name_el.text or "").strip() if name_el is not None else code
        instruments = tuple(
            (i.text or "").strip()
            for i in loc.iter("Instrument")
            if i.text
        )
        out.append(Location(code=code, display_name=name, instruments=instruments))
    return out


def parse_scan_codes(xml: str) -> list[ScanCode]:
    """Extract <ScanType> entries from the scanner XML."""
    root = _root(xml)
    out: list[ScanCode] = []
    for st in root.iter("ScanType"):
        code_el = st.find("scanCode")
        name_el = st.find("displayName")
        if code_el is None or not code_el.text:
            continue
        code = code_el.text.strip()
        name = (name_el.text or "").strip() if name_el is not None else code
        instruments = tuple(
            (i.text or "").strip()
            for i in st.iter("Instrument")
            if i.text
        )
        out.append(ScanCode(code=code, display_name=name, instruments=instruments))
    return out


def stock_locations(xml: str) -> list[Location]:
    """Subset of `parse_locations(xml)` that lists STK in its instrument list,
    OR (defensively) whose code begins with STK.* since some IBKR builds omit
    per-location instrument lists.
    """
    locs = parse_locations(xml)
    return [
        loc
        for loc in locs
        if (not loc.instruments and loc.code.startswith("STK"))
        or "STK" in loc.instruments
        or loc.code.startswith("STK")
    ]


def stock_scan_codes(xml: str) -> list[ScanCode]:
    """Subset of scan codes that apply to stocks."""
    codes = parse_scan_codes(xml)
    return [
        sc
        for sc in codes
        if (not sc.instruments) or ("STK" in sc.instruments)
    ]


def has_location(xml: str, code: str) -> bool:
    return any(loc.code == code for loc in parse_locations(xml))


def has_scan_code(xml: str, code: str) -> bool:
    return any(sc.code == code for sc in parse_scan_codes(xml))


def discover_tsx_location(xml: str) -> str | None:
    """Pick a Canada/TSX location code from the XML by priority + keyword match.

    Returns None when nothing plausible is present — caller should then try
    `probe_tsx_location()` against the live IB connection.
    """
    locs = {loc.code for loc in parse_locations(xml)}
    for candidate in _TSX_CANDIDATES_PRIORITY:
        if candidate in locs:
            log.info("TSX location discovered from XML: %s", candidate)
            return candidate
    for code in sorted(locs):
        upper = code.upper()
        if any(kw in upper for kw in _TSX_KEYWORDS):
            log.info("TSX location discovered from XML (keyword match): %s", code)
            return code
    log.warning(
        "No Canada/TSX location code found in scanner XML. "
        "Will probe live or skip TSX bucket."
    )
    return None


async def probe_tsx_location(ib: IB, xml: str | None = None) -> str | None:
    """Best-effort runtime probe for a TSX location code.

    Tries the top-two candidates in order with a tiny scan (numberOfRows=1).
    Keeps the first that returns >= 1 result. Returns None if all fail.
    """
    # Late import to keep this module importable without ib_async installed,
    # which matters for parse-only unit tests.
    from ib_async import ScannerSubscription

    candidates = list(_TSX_CANDIDATES_PRIORITY)
    if xml:
        xml_locs = {loc.code for loc in parse_locations(xml)}
        # Prefer candidates that at least *appear* in the XML.
        candidates = [c for c in candidates if c in xml_locs] + [
            c for c in candidates if c not in xml_locs
        ]
    for candidate in candidates:
        sub = ScannerSubscription(
            instrument="STK",
            locationCode=candidate,
            scanCode="MOST_ACTIVE",
            numberOfRows=1,
        )
        try:
            data = await ib.reqScannerDataAsync(sub, [], [])
        except Exception as exc:
            log.debug("TSX probe failed for %s: %s", candidate, exc)
            continue
        if data:
            log.info("TSX location confirmed via live probe: %s", candidate)
            return candidate
        log.debug("TSX probe returned no rows for %s", candidate)
    log.warning("All TSX location probes failed; skipping TSX bucket.")
    return None


def cached_tsx_location_path(cache_dir: Path) -> Path:
    return cache_dir / "tsx_location.txt"


def read_cached_tsx(cache_dir: Path) -> str | None:
    p = cached_tsx_location_path(cache_dir)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip() or None


def write_cached_tsx(code: str, cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_tsx_location_path(cache_dir).write_text(code, encoding="utf-8")


def discover_uk_eu_location(xml: str) -> str | None:
    """Like `discover_tsx_location` but for the UK/EU bucket."""
    locs = {loc.code for loc in parse_locations(xml)}
    for candidate in _UK_EU_CANDIDATES_PRIORITY:
        if candidate in locs:
            log.info("UK/EU location discovered from XML: %s", candidate)
            return candidate
    for code in sorted(locs):
        upper = code.upper()
        if any(kw in upper for kw in _UK_EU_KEYWORDS):
            log.info("UK/EU location discovered from XML (keyword match): %s", code)
            return code
    log.warning("No UK/EU location code found in scanner XML.")
    return None


async def probe_uk_eu_location(ib: IB, xml: str | None = None) -> str | None:
    """Best-effort runtime probe for a UK/EU location code."""
    from ib_async import ScannerSubscription

    candidates = list(_UK_EU_CANDIDATES_PRIORITY)
    if xml:
        xml_locs = {loc.code for loc in parse_locations(xml)}
        candidates = [c for c in candidates if c in xml_locs] + [
            c for c in candidates if c not in xml_locs
        ]
    for candidate in candidates:
        sub = ScannerSubscription(
            instrument="STK",
            locationCode=candidate,
            scanCode="MOST_ACTIVE",
            numberOfRows=1,
        )
        try:
            data = await ib.reqScannerDataAsync(sub, [], [])
        except Exception as exc:
            log.debug("UK/EU probe failed for %s: %s", candidate, exc)
            continue
        if data:
            log.info("UK/EU location confirmed via live probe: %s", candidate)
            return candidate
        log.debug("UK/EU probe returned no rows for %s", candidate)
    log.warning("All UK/EU location probes failed; skipping UK/EU bucket.")
    return None


def cached_uk_eu_location_path(cache_dir: Path) -> Path:
    return cache_dir / "uk_eu_location.txt"


def read_cached_uk_eu(cache_dir: Path) -> str | None:
    p = cached_uk_eu_location_path(cache_dir)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip() or None


def write_cached_uk_eu(code: str, cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_uk_eu_location_path(cache_dir).write_text(code, encoding="utf-8")


def render_stock_scanner_info_text(xml: str) -> str:
    """Plain-text summary suitable for tests / logs; CLI uses Rich tables."""
    locs = stock_locations(xml)
    codes = stock_scan_codes(xml)
    lines = [
        "=== STK Locations ===",
        *(f"  {loc.code:<32} {loc.display_name}" for loc in locs),
        "",
        "=== STK Scan Codes ===",
        *(f"  {sc.code:<32} {sc.display_name}" for sc in codes),
    ]
    return "\n".join(lines)


def print_stock_scanner_info(xml: str) -> None:
    """Pretty-print location + scan code tables to the terminal."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    locs = stock_locations(xml)
    codes = stock_scan_codes(xml)

    t1 = Table(title=f"STK Locations ({len(locs)})", show_lines=False)
    t1.add_column("Code", style="cyan", no_wrap=True)
    t1.add_column("Display name")
    for loc in locs:
        t1.add_row(loc.code, loc.display_name)
    console.print(t1)

    t2 = Table(title=f"STK Scan Codes ({len(codes)})", show_lines=False)
    t2.add_column("Code", style="green", no_wrap=True)
    t2.add_column("Display name")
    for sc in codes:
        t2.add_row(sc.code, sc.display_name)
    console.print(t2)
