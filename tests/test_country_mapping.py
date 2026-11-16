"""Tests for the config-driven publisher→country attribution."""

from __future__ import annotations

from congiuntura_live.processor import ReleaseProcessor
from congiuntura_live.settings import ProcessingConfig, load_feeds_config

EXPECTED = {
    "eurostat": "Euro area",
    "istat": "Italy",
    "ine": "Spain",
    "insee": "France",
    "destatis": "Germany",
    "cso": "Ireland",
    "dgecofin": "Euro area",
}


def _agencies():
    return load_feeds_config("config/feeds.toml")


def test_mapping_values():
    agencies = _agencies()
    assert {slug: cfg.country for slug, cfg in agencies.items()} == EXPECTED


def test_every_agency_declares_a_country():
    """Coverage guard: an agency without a country key fails config load."""
    for slug, agency in _agencies().items():
        assert agency.country, f"{slug} has no country in feeds.toml"


def test_no_other_option():
    """'Other' is never a valid derived country."""
    assert "Other" not in {cfg.country for cfg in _agencies().values()}


def test_filter_choices_are_deduplicated():
    """Several agencies may share an area (Eurostat + DG EcoFin) — the
    filter dropdown must list each area once, in TOML order."""
    choices = list(dict.fromkeys(cfg.country for cfg in _agencies().values()))
    assert choices == ["Euro area", "Italy", "Spain", "France", "Germany", "Ireland"]


def test_unknown_publisher_maps_to_empty():
    processor = ReleaseProcessor(ProcessingConfig())
    assert processor.country_for("unknown-agency") == ""
    assert processor.country_for("dgecofin") == "Euro area"
