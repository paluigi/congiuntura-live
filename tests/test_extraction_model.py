"""Tests for the LLMExtraction model loading and introspection."""

from __future__ import annotations

import pytest

from congiuntura_live.settings import load_extraction_model


@pytest.fixture
def model_class():
    return load_extraction_model("config/extraction_model.py")


class TestModelLoading:
    def test_model_loads_from_python_file(self, model_class):
        assert model_class.__name__ == "LLMExtraction"

    def test_model_is_pydantic(self, model_class):
        from pydantic import BaseModel

        assert issubclass(model_class, BaseModel)

    def test_model_has_expected_fields(self, model_class):
        fields = set(model_class.model_fields.keys())
        assert fields == {"topics", "sentiment", "title_en", "summary_en", "key_figures"}

    def test_country_is_not_an_llm_field(self, model_class):
        """Country is derived deterministically from the publisher — the
        LLM must not generate it."""
        assert "country" not in model_class.model_fields

    def test_no_auto_fields_in_model(self, model_class):
        """The LLM must NOT see url/date/publisher fields."""
        fields = set(model_class.model_fields.keys())
        forbidden = {"url", "url_hash", "published", "fetched_at", "publisher", "processing_model"}
        assert not (fields & forbidden), "Auto fields leaked into LLM model"

    @staticmethod
    def _topic_choices(model_class) -> list[str]:
        """Unwrap the choices from the list[Literal[...]] topics annotation."""
        import typing

        topics_ann = model_class.model_fields["topics"].annotation
        assert typing.get_origin(topics_ann) is list, "topics must be a list field"
        (literal,) = typing.get_args(topics_ann)
        return list(typing.get_args(literal))

    def test_topic_has_import_export_prices(self, model_class):
        choices = self._topic_choices(model_class)
        assert "Import prices" in choices
        assert "Export prices" in choices

    def test_topic_has_expected_categories(self, model_class):
        choices = self._topic_choices(model_class)
        expected = {"Consumer prices", "Producer prices", "GDP", "Industrial production"}
        assert expected.issubset(set(choices))

    def test_topics_json_schema_is_openai_strict_compatible(self, model_class):
        """The schema must express an enum-constrained array (minItems) —
        the shape OpenAI-compatible structured outputs enforce."""
        schema = model_class.model_json_schema()
        topics_schema = schema["properties"]["topics"]

        assert topics_schema["type"] == "array"
        assert set(topics_schema["items"]["enum"]) == set(self._topic_choices(model_class))
        assert schema["required"] == ["topics", "sentiment", "title_en", "summary_en", "key_figures"]
        assert topics_schema.get("minItems", 1) >= 1

    def test_sentiment_choices(self, model_class):
        import typing

        sentiment_ann = model_class.model_fields["sentiment"].annotation
        choices = typing.get_args(sentiment_ann)
        assert set(choices) == {"positive", "negative", "neutral"}

    def test_can_instantiate_with_valid_data(self, model_class):
        instance = model_class(
            topics=["GDP", "International trade"],
            sentiment="neutral",
            title_en="GDP grew by 0.3% in Q1 2025",
            summary_en="GDP rose by 0.3% in Q1 2025.",
            key_figures="+0.3% QoQ, +0.9% YoY",
        )
        assert instance.topics == ["GDP", "International trade"]
        assert instance.sentiment == "neutral"

    def test_rejects_invalid_topic(self, model_class):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            model_class(
                topics=["Invalid topic"],
                sentiment="neutral",
                summary_en="test",
                key_figures="test",
            )

    def test_rejects_empty_topics(self, model_class):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            model_class(
                topics=[],
                sentiment="neutral",
                summary_en="test",
                key_figures="test",
            )
