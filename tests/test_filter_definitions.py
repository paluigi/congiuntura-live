"""Tests for the UI filter auto-generation from the extraction model."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from congiuntura_live.app import _build_filter_definitions
from congiuntura_live.processor import PUBLISHER_COUNTRY


class _Model(BaseModel):
    topics: list[Literal["GDP", "Labour market"]]
    sentiment: Literal["positive", "neutral"]
    title_en: str
    plain_list: list[str]


class _StaleModelWithCountry(BaseModel):
    """Simulates a stale extraction_model.py that still defines country."""

    topics: list[Literal["GDP", "Labour market"]]
    country: Literal["Euro area", "Italy", "Other"]
    sentiment: Literal["positive", "neutral"]


def test_literal_fields_and_injected_country_filter(monkeypatch):
    monkeypatch.setattr("congiuntura_live.app._extraction_model_class", _Model)

    filters = _build_filter_definitions()

    # Country (derived from the publisher) is injected after topics,
    # even though it is no longer an LLM model field.
    assert [f["name"] for f in filters] == ["topics", "country", "sentiment"]
    assert filters[0]["choices"] == ["GDP", "Labour market"]
    assert filters[1]["choices"] == list(PUBLISHER_COUNTRY.values())
    assert filters[2]["choices"] == ["positive", "neutral"]


def test_without_model_country_filter_still_present(monkeypatch):
    monkeypatch.setattr("congiuntura_live.app._extraction_model_class", None)

    filters = _build_filter_definitions()

    assert [f["name"] for f in filters] == ["country"]
    assert filters[0]["choices"] == list(PUBLISHER_COUNTRY.values())


def test_stale_model_country_field_is_ignored(monkeypatch):
    """A stale extraction model still defining country must not produce a
    second, model-derived country filter."""
    monkeypatch.setattr("congiuntura_live.app._extraction_model_class", _StaleModelWithCountry)

    filters = _build_filter_definitions()

    country_filters = [f for f in filters if f["name"] == "country"]
    assert [f["name"] for f in filters] == ["topics", "country", "sentiment"]
    assert len(country_filters) == 1
    assert country_filters[0]["choices"] == list(PUBLISHER_COUNTRY.values())
    assert "Other" not in country_filters[0]["choices"]
