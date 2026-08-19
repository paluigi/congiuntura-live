"""Tests for the deterministic publisher→country mapping."""

from __future__ import annotations

import tomllib
from pathlib import Path

from congiuntura_live.processor import PUBLISHER_COUNTRY

EXPECTED = {
    "eurostat": "Euro area",
    "istat": "Italy",
    "ine": "Spain",
    "insee": "France",
    "destatis": "Germany",
    "cso": "Ireland",
}


def test_mapping_values():
    assert PUBLISHER_COUNTRY == EXPECTED


def test_no_other_option():
    """'Other' is never a valid derived country."""
    assert "Other" not in PUBLISHER_COUNTRY.values()


def test_countries_are_unique():
    assert len(set(PUBLISHER_COUNTRY.values())) == len(PUBLISHER_COUNTRY)


def test_unknown_publisher_maps_to_empty():
    assert PUBLISHER_COUNTRY.get("unknown-agency", "") == ""


def test_every_configured_publisher_is_mapped():
    """Coverage guard: adding a feed agency without a country mapping fails here."""
    feeds = tomllib.loads((Path(__file__).parent.parent / "config" / "feeds.toml").read_text())
    agency_slugs = {k for k, v in feeds.items() if isinstance(v, dict) and "feeds" in v}
    assert agency_slugs, "no agencies found in feeds.toml — parser drifted"
    assert agency_slugs == set(PUBLISHER_COUNTRY)
