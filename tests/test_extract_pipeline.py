"""GRP-25: untagged-item selection + pipeline orchestration.

Drives :func:`grepify.extract.pipeline.run_extract_pipeline` end to end
against a scripted LLM client (no network) — mirrors ``test_backfill.py``'s
shape for the sibling ``run_fallback_backfill`` orchestrator.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from grepify.clock import FixedClock
from grepify.config.schemas import KeywordsConfig
from grepify.extract.pipeline import run_extract_pipeline, select_untagged_items
from grepify.keywords import KeywordRules
from grepify.llm.client import LlmClient, RetryPolicy
from grepify.models import ExtractionMethod
from tests.conftest import (
    FakeFallbackExtractor,
    ScriptedCompletionTransport,
    envelope_response,
    make_item,
    make_keyword,
)

_CLOCK = FixedClock(datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC))
_NO_RULES = KeywordRules.from_config(KeywordsConfig())


# --- select_untagged_items ----------------------------------------------------


def test_selects_items_with_no_keyword_rows_at_all() -> None:
    items = [make_item("a"), make_item("b"), make_item("c")]
    keywords = [
        make_keyword("a", "kw"),  # method='llm' -> tagged, excluded
        make_keyword("b", "kw").model_copy(update={"method": ExtractionMethod.FALLBACK}),
        # "c" has no keyword rows -> untagged, selected
    ]
    selected = select_untagged_items(items, keywords)
    assert [item.item_id for item in selected] == ["c"]


def test_no_items_no_keywords_returns_empty() -> None:
    assert select_untagged_items([], []) == []


def test_orphaned_keyword_rows_do_not_fabricate_phantom_items() -> None:
    items = [make_item("a")]
    keywords = [make_keyword("orphan", "kw")]
    assert [i.item_id for i in select_untagged_items(items, keywords)] == ["a"]


# --- run_extract_pipeline -----------------------------------------------------


def _kw_text(mapping: dict[str, list[str]]) -> str:
    return json.dumps([{"item_id": k, "keywords": v} for k, v in mapping.items()])


def _client(script: list[str]) -> LlmClient:
    transport = ScriptedCompletionTransport([envelope_response(s) for s in script])
    return LlmClient(
        model="test-model",
        base_url="https://x/v1",
        api_key="k",
        log_sink=lambda _entry: None,
        clock=_CLOCK,
        transport=transport,
        retry=RetryPolicy(sleep=lambda _s: None, rng=lambda: 0.0),
    )


def test_only_untagged_items_reach_the_llm() -> None:
    items = [make_item("a"), make_item("b")]
    keywords = [make_keyword("b", "already-tagged")]
    client = _client([_kw_text({"a": ["genai"]})])

    summary, rows = run_extract_pipeline(
        items,
        keywords,
        client,
        run_id="r1",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
        rules=_NO_RULES,
    )

    assert summary.items_selected == 1
    assert summary.batches_llm == 1
    assert {row.item_id for row in rows} == {"a"}
    assert [row.keyword for row in rows] == ["genai"]


def test_keywords_are_normalized_before_being_returned() -> None:
    items = [make_item("a")]
    client = _client([_kw_text({"a": ["  Gen   AI!! "]})])

    _summary, rows = run_extract_pipeline(
        items,
        [],
        client,
        run_id="r1",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
        rules=_NO_RULES,
    )
    assert [row.keyword for row in rows] == ["gen ai"]


def test_alias_map_is_applied_to_llm_output() -> None:
    items = [make_item("a")]
    client = _client([_kw_text({"a": ["Gen AI"]})])
    rules = KeywordRules.from_config(KeywordsConfig(aliases={"gen ai": "genai"}))

    _summary, rows = run_extract_pipeline(
        items, [], client, run_id="r1", clock=_CLOCK, fallback=FakeFallbackExtractor(), rules=rules
    )
    assert [row.keyword for row in rows] == ["genai"]


def test_muted_keywords_are_dropped_and_counted() -> None:
    items = [make_item("a")]
    client = _client([_kw_text({"a": ["genai", "webinar"]})])
    rules = KeywordRules.from_config(KeywordsConfig(mute=["webinar"]))

    summary, rows = run_extract_pipeline(
        items, [], client, run_id="r1", clock=_CLOCK, fallback=FakeFallbackExtractor(), rules=rules
    )
    assert [row.keyword for row in rows] == ["genai"]
    assert summary.muted_count == 1


def test_item_muted_down_to_zero_keywords_is_flagged_no_keywords() -> None:
    items = [make_item("a")]
    client = _client([_kw_text({"a": ["webinar"]})])
    rules = KeywordRules.from_config(KeywordsConfig(mute=["webinar"]))

    summary, rows = run_extract_pipeline(
        items, [], client, run_id="r1", clock=_CLOCK, fallback=FakeFallbackExtractor(), rules=rules
    )
    assert rows == []
    assert summary.no_keywords_item_ids == ["a"]


def test_llm_empty_result_is_flagged_no_keywords_not_raised() -> None:
    items = [make_item("a")]
    client = _client([_kw_text({"a": []})])

    summary, rows = run_extract_pipeline(
        items,
        [],
        client,
        run_id="r1",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
        rules=_NO_RULES,
    )
    assert rows == []
    assert summary.no_keywords_item_ids == ["a"]


def test_force_reextracts_already_tagged_items() -> None:
    items = [make_item("a")]
    keywords = [make_keyword("a", "old-keyword")]
    client = _client([_kw_text({"a": ["fresh-keyword"]})])

    summary, rows = run_extract_pipeline(
        items,
        keywords,
        client,
        run_id="r1",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
        rules=_NO_RULES,
        force=True,
    )
    assert summary.items_selected == 1
    assert [row.keyword for row in rows] == ["fresh-keyword"]


def test_without_force_already_tagged_items_are_not_reselected() -> None:
    items = [make_item("a")]
    keywords = [make_keyword("a", "old-keyword")]
    client = _client([])

    summary, rows = run_extract_pipeline(
        items,
        keywords,
        client,
        run_id="r1",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
        rules=_NO_RULES,
    )
    assert summary.items_selected == 0
    assert rows == []


def test_budget_exhaustion_flows_through_to_summary() -> None:
    items = [make_item("a"), make_item("b")]
    client = LlmClient(
        model="test-model",
        base_url="https://x/v1",
        api_key="k",
        log_sink=lambda _entry: None,
        clock=_CLOCK,
        transport=ScriptedCompletionTransport([envelope_response(_kw_text({"a": ["genai"]}))]),
        max_calls_per_run=1,
        retry=RetryPolicy(sleep=lambda _s: None, rng=lambda: 0.0),
    )

    summary, rows = run_extract_pipeline(
        items,
        [],
        client,
        run_id="r1",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
        rules=_NO_RULES,
        max_items_per_call=1,
    )
    assert summary.budget_exhausted is True
    assert summary.batches_llm == 1
    assert summary.batches_fallback == 1
    assert {row.item_id for row in rows} == {"a", "b"}
