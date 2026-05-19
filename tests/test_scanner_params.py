"""Unit tests for scanner_params XML parsing + TSX discovery."""

from __future__ import annotations

from ibkr_vol_screener.scanner_params import (
    discover_tsx_location,
    discover_uk_eu_location,
    has_location,
    has_scan_code,
    parse_locations,
    parse_scan_codes,
    stock_locations,
    stock_scan_codes,
)


def test_parse_locations_extracts_all(fixtures_dir):
    xml = (fixtures_dir / "scanner_params_sample.xml").read_text(encoding="utf-8")
    locs = parse_locations(xml)
    codes = {loc.code for loc in locs}
    assert {"STK.US.MAJOR", "STK.US.MINOR", "STK.NA.CANADA", "FUT.US"}.issubset(codes)


def test_stock_locations_filters_out_futures(fixtures_dir):
    xml = (fixtures_dir / "scanner_params_sample.xml").read_text(encoding="utf-8")
    codes = {loc.code for loc in stock_locations(xml)}
    assert "FUT.US" not in codes
    assert "STK.US.MAJOR" in codes


def test_parse_scan_codes(fixtures_dir):
    xml = (fixtures_dir / "scanner_params_sample.xml").read_text(encoding="utf-8")
    codes = {sc.code for sc in parse_scan_codes(xml)}
    assert {"HOT_BY_VOLUME", "TOP_PERC_GAIN", "TOP_PERC_LOSE", "MOST_ACTIVE"}.issubset(codes)


def test_stock_scan_codes_filters_out_futures(fixtures_dir):
    xml = (fixtures_dir / "scanner_params_sample.xml").read_text(encoding="utf-8")
    codes = {sc.code for sc in stock_scan_codes(xml)}
    assert "SOME_FUTURES_ONLY_CODE" not in codes


def test_has_location_and_scan_code(fixtures_dir):
    xml = (fixtures_dir / "scanner_params_sample.xml").read_text(encoding="utf-8")
    assert has_location(xml, "STK.US.MAJOR")
    assert not has_location(xml, "STK.UK.MAJOR")
    assert has_scan_code(xml, "HOT_BY_VOLUME")
    assert not has_scan_code(xml, "BOGUS")


def test_discover_tsx_location_finds_canada(fixtures_dir):
    xml = (fixtures_dir / "scanner_params_sample.xml").read_text(encoding="utf-8")
    assert discover_tsx_location(xml) == "STK.NA.CANADA"


def test_discover_tsx_location_returns_none_when_absent(fixtures_dir):
    xml = (fixtures_dir / "scanner_params_no_tsx.xml").read_text(encoding="utf-8")
    assert discover_tsx_location(xml) is None


def test_discover_uk_eu_prefers_lse_country_specific(fixtures_dir):
    """STK.EU.LSE wins because IBKR scanners aren't configured for the broad
    STK.EU code (verified live 2026-05-19)."""
    xml = (fixtures_dir / "scanner_params_sample.xml").read_text(encoding="utf-8")
    assert discover_uk_eu_location(xml) == "STK.EU.LSE"


def test_discover_uk_eu_returns_none_when_absent(fixtures_dir):
    xml = (fixtures_dir / "scanner_params_no_tsx.xml").read_text(encoding="utf-8")
    assert discover_uk_eu_location(xml) is None
