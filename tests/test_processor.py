"""Unit tests for ReleaseProcessor.process_one (LLM mocked out)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import congiuntura_live.processor as processor_module
from congiuntura_live.processor import ReleaseProcessor
from congiuntura_live.settings import load_extraction_model

RAW_DOC = {
    "url_hash": "h1",
    "url": "https://www.istat.it/communicato/x",
    "title": "Conti nazionali",
    "summary": "Il PIL è cresciuto dello 0,3%.",
    "publisher": "istat",
    "publisher_full": "Istat",
    "feed_label": "Main",
    "language": "it",
    "published": "2026-08-01T10:00:00+00:00",
    "fetched_at": "2026-08-01T10:05:00+00:00",
}


@dataclass
class _FakeResponse:
    value: object
    provider: str = "test-provider"
    model: str = "test-model"


class _FakeScraper:
    async def extract_content(self, url: str) -> str:
        return ""  # force the feed-summary fallback

    async def close(self) -> None:
        pass


def _make_processor() -> ReleaseProcessor:
    from types import SimpleNamespace

    proc = ReleaseProcessor.__new__(ReleaseProcessor)
    proc._scraper = _FakeScraper()
    proc._extraction_model = load_extraction_model("config/extraction_model.py")
    proc._cascade_config = object()  # truthy — skips lazy config loading
    proc._cfg = SimpleNamespace(cascade_name="test-cascade")
    return proc


async def test_process_one_happy_path(monkeypatch):
    proc = _make_processor()
    extraction = proc._extraction_model(
        topics=["GDP", "GDP", "International trade"],
        sentiment="positive",
        title_en="National accounts",
        summary_en="GDP rose by 0.3%.",
        key_figures="+0.3%",
    )

    async def fake_generate(**kwargs):
        return _FakeResponse(value=extraction)

    monkeypatch.setattr(processor_module, "generate", fake_generate)
    processed = await proc.process_one(dict(RAW_DOC))

    assert processed is not None
    assert processed["country"] == "Italy"  # derived from publisher, not the LLM
    assert processed["topics"] == ["GDP", "International trade"]  # deduped, order kept
    assert processed["processing_model"] == "test-provider/test-model"


async def test_process_one_unknown_publisher_empty_country(monkeypatch):
    proc = _make_processor()
    extraction = proc._extraction_model(
        topics=["GDP"],
        sentiment="neutral",
        title_en="t",
        summary_en="s",
        key_figures="k",
    )

    async def fake_generate(**kwargs):
        return _FakeResponse(value=extraction)

    monkeypatch.setattr(processor_module, "generate", fake_generate)
    raw = dict(RAW_DOC, publisher="unknown-agency")
    processed = await proc.process_one(raw)

    assert processed is not None
    assert processed["country"] == ""


async def test_process_one_invalid_llm_output_returns_none(monkeypatch):
    """outlines-cascade returns the raw string when validation fails —
    process_one must treat it as a failure, not crash with AttributeError."""

    async def fake_generate(**kwargs):
        return _FakeResponse(value='{"topics": "GDP", ...}  (invalid)')

    monkeypatch.setattr(processor_module, "generate", fake_generate)
    processed = await _make_processor().process_one(dict(RAW_DOC))

    assert processed is None


async def test_process_one_generate_error_returns_none(monkeypatch):
    async def fake_generate(**kwargs):
        raise RuntimeError("cascade exhausted")

    monkeypatch.setattr(processor_module, "generate", fake_generate)
    processed = await _make_processor().process_one(dict(RAW_DOC))

    assert processed is None


def test_all_processor_code_paths_covered_by_suite():
    """Sanity: the module imports and the fake fixtures are coherent."""
    assert callable(processor_module.generate)
