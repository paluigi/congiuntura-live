"""Tests for the UI filter auto-generation from the extraction model."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from congiuntura_live.app import _build_filter_definitions


class _Model(BaseModel):
    topics: list[Literal["GDP", "Labour market"]]
    country: Literal["Italy", "Euro area"]
    title_en: str
    plain_list: list[str]


def test_literal_fields_become_filters(monkeypatch):
    monkeypatch.setattr("congiuntura_live.app._extraction_model_class", _Model)

    filters = {f["name"]: f for f in _build_filter_definitions()}

    assert set(filters) == {"topics", "country"}

    # list[Literal[...]] unwraps to the inner choices (multi-tag filter)
    assert filters["topics"]["choices"] == ["GDP", "Labour market"]
    # bare Literal keeps working (single-choice filter)
    assert filters["country"]["choices"] == ["Italy", "Euro area"]


def test_without_model_returns_empty(monkeypatch):
    monkeypatch.setattr("congiuntura_live.app._extraction_model_class", None)
    assert _build_filter_definitions() == []
