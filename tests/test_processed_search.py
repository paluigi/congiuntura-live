"""Unit tests for the processed-release query builder (free-text search).

``_build_processed_query`` is a static method, so no MongoDB instance is
needed — these tests only assert the shape of the generated query.
"""

from __future__ import annotations

import re

from congiuntura_live.repository import PressReleaseRepository

build_query = PressReleaseRepository._build_processed_query

TEXT_FIELDS = ("title_en", "summary_en", "key_figures")


def test_q_alone_builds_or_over_text_fields():
    query = build_query({"q": "inflation"})

    or_clauses = query["$or"]
    assert {next(iter(clause)) for clause in or_clauses} == set(TEXT_FIELDS)
    for clause in or_clauses:
        assert clause[next(iter(clause))] == {"$regex": "inflation", "$options": "i"}


def test_q_combines_with_select_and_date_filters():
    query = build_query({
        "publisher": ["istat", "eurostat"],
        "topics": ["GDP", "Labour market"],
        "sentiment": ["positive"],
        "date_from": "2026-08-01T00:00:00+00:00",
        "date_to": "2026-08-18T00:00:00+00:00",
        "q": "growth",
    })

    assert query["publisher"] == {"$in": ["istat", "eurostat"]}
    # topics is array-valued: $in matches docs containing ANY selected value (OR)
    assert query["topics"] == {"$in": ["GDP", "Labour market"]}
    assert query["sentiment"] == {"$in": ["positive"]}
    assert query["published"] == {
        "$gte": "2026-08-01T00:00:00+00:00",
        "$lte": "2026-08-18T00:00:00+00:00",
    }
    assert len(query["$or"]) == 3


def test_without_q_query_is_unchanged():
    filters = {"publisher": ["istat"], "date_from": "2026-08-01T00:00:00+00:00"}
    assert build_query(filters) == {
        "publisher": {"$in": ["istat"]},
        "published": {"$gte": "2026-08-01T00:00:00+00:00"},
    }
    assert build_query({}) == {}


def test_q_special_characters_are_escaped():
    fragment = "+0.3% (flash)"
    query = build_query({"q": fragment})

    for clause in query["$or"]:
        regex = clause[next(iter(clause))]["$regex"]
        assert regex == re.escape(fragment)


def test_q_case_insensitive_flag_is_set():
    query = build_query({"q": "hicp"})

    for clause in query["$or"]:
        assert clause[next(iter(clause))]["$options"] == "i"
