"""GRP-22: fallback re-extraction backfill — selection + orchestration."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from grepify.clock import FixedClock
from grepify.extract.backfill import run_fallback_backfill, select_fallback_items
from grepify.llm.client import LlmClient, RetryPolicy
from grepify.models import ExtractionMethod, ItemKeyword, LlmLogEntry
from tests.conftest import (
    FakeFallbackExtractor,
    ScriptedCompletionTransport,
    envelope_response,
    make_item,
    make_keyword,
)

_CLOCK = FixedClock(datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC))


def _fallback_row(item_id: str, keyword: str = "kw") -> ItemKeyword:
    row = make_keyword(item_id, keyword)
    return row.model_copy(update={"method": ExtractionMethod.FALLBACK, "model": None})


# --- select_fallback_items ----------------------------------------------------


def test_selects_items_whose_keywords_are_entirely_fallback() -> None:
    items = [make_item("a"), make_item("b"), make_item("c")]
    keywords = [
        _fallback_row("a", "kw1"),
        _fallback_row("a", "kw2"),
        make_keyword("b", "kw"),  # method='llm' by default -> not eligible
        # "c" has no keyword rows at all -> not eligible (untagged, not fallback)
    ]
    selected = select_fallback_items(items, keywords)
    assert [item.item_id for item in selected] == ["a"]


def test_item_with_any_llm_row_is_excluded_even_if_also_has_fallback_rows() -> None:
    items = [make_item("a")]
    keywords = [_fallback_row("a", "kw1"), make_keyword("a", "kw2")]
    assert select_fallback_items(items, keywords) == []


def test_untagged_item_is_not_selected() -> None:
    items = [make_item("a")]
    assert select_fallback_items(items, []) == []


def test_no_items_no_keywords_returns_empty() -> None:
    assert select_fallback_items([], []) == []


# --- run_fallback_backfill -----------------------------------------------------


def _kw_text(mapping: dict[str, list[str]]) -> str:
    return json.dumps([{"item_id": k, "keywords": v} for k, v in mapping.items()])


def _client(script: list[str]) -> tuple[LlmClient, list[LlmLogEntry]]:
    transport = ScriptedCompletionTransport([envelope_response(s) for s in script])
    logs: list[LlmLogEntry] = []
    client = LlmClient(
        model="test-model",
        base_url="https://x/v1",
        api_key="k",
        log_sink=logs.append,
        clock=_CLOCK,
        transport=transport,
        retry=RetryPolicy(sleep=lambda _s: None, rng=lambda: 0.0),
    )
    return client, logs


def test_only_fallback_only_items_reach_the_llm() -> None:
    items = [make_item("a"), make_item("b")]
    keywords = [_fallback_row("a", "old-fallback"), make_keyword("b", "already-llm")]
    client, _logs = _client([_kw_text({"a": ["genai"]})])

    result = run_fallback_backfill(
        items,
        keywords,
        client,
        run_id="r1",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
    )

    assert result.batches_total == 1
    assert result.batches_llm == 1
    assert {kw.item_id for kw in result.keywords} == {"a"}
    assert [kw.keyword for kw in result.keywords] == ["genai"]
    assert all(kw.method is ExtractionMethod.LLM for kw in result.keywords)


def test_no_candidates_makes_no_llm_calls() -> None:
    items = [make_item("a")]
    keywords = [make_keyword("a", "already-llm")]
    client, transport_logs = _client([])

    result = run_fallback_backfill(
        items,
        keywords,
        client,
        run_id="r1",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
    )
    assert result.batches_total == 0
    assert result.keywords == []
    assert transport_logs == []


def test_still_fallback_on_repeated_llm_failure() -> None:
    items = [make_item("a")]
    keywords = [_fallback_row("a")]
    fallback = FakeFallbackExtractor({"a": ["fb-again"]})
    client, _logs = _client(["not json", "still not json"])

    result = run_fallback_backfill(
        items, keywords, client, run_id="r1", clock=_CLOCK, fallback=fallback
    )
    assert result.batches_fallback == 1
    assert [kw.keyword for kw in result.keywords] == ["fb-again"]
    assert all(kw.method is ExtractionMethod.FALLBACK for kw in result.keywords)
