"""GRP-21: extract batcher — batching, retry-then-fallback, budget cascade.

Drives :func:`grepify.extract.run_extract` end to end against a scripted LLM
client (no network) and a fake fallback extractor standing in for GRP-22.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from grepify.clock import FixedClock
from grepify.extract import run_extract
from grepify.ingest.http import HttpResponse
from grepify.llm import LlmClient
from grepify.llm.client import RetryPolicy
from grepify.models import ExtractionMethod, Item, LlmLogEntry, SourceKind
from tests.conftest import FakeFallbackExtractor, ScriptedCompletionTransport, envelope_response

_CLOCK = FixedClock(datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC))


def _item(item_id: str) -> Item:
    return Item(
        item_id=item_id,
        source_id="src-1",
        kind=SourceKind.RSS,
        external_id=item_id,
        canonical_url=f"https://example.com/{item_id}",
        title=f"title {item_id}",
        summary=f"summary for {item_id}",
        published_at="2026-07-08T09:00:00+00:00",
        fetched_at="2026-07-08T10:00:00+00:00",
        content_hash=f"hash-{item_id}",
    )


def _kw_text(mapping: dict[str, list[str]]) -> str:
    return json.dumps([{"item_id": k, "keywords": v} for k, v in mapping.items()])


def _client(
    script: list[str | HttpResponse | Exception], *, max_calls: int | None = None
) -> tuple[LlmClient, ScriptedCompletionTransport, list[LlmLogEntry]]:
    responses: list[HttpResponse | Exception] = [
        s if isinstance(s, (HttpResponse, Exception)) else envelope_response(s) for s in script
    ]
    transport = ScriptedCompletionTransport(responses)
    logs: list[LlmLogEntry] = []
    client = LlmClient(
        model="test-model",
        base_url="https://x/v1",
        api_key="k",
        log_sink=logs.append,
        clock=_CLOCK,
        transport=transport,
        max_calls_per_run=max_calls,
        retry=RetryPolicy(sleep=lambda _s: None, rng=lambda: 0.0),
    )
    return client, transport, logs


# --- happy path --------------------------------------------------------------


def test_valid_single_batch_produces_llm_rows_with_rank_and_model() -> None:
    client, transport, logs = _client([_kw_text({"a": ["genai", "agents"], "b": ["dbt"]})])
    result = run_extract(
        [_item("a"), _item("b")],
        client,
        run_id="r1",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
    )

    assert result.batches_total == 1
    assert result.batches_llm == 1
    assert result.batches_fallback == 0
    assert result.llm_retries == 0
    assert result.budget_exhausted is False
    assert len(transport.posts) == 1
    assert len(logs) == 1 and logs[0].status == "ok"

    by_item = {(k.item_id, k.rank): k for k in result.keywords}
    assert by_item[("a", 1)].keyword == "genai"
    assert by_item[("a", 1)].method is ExtractionMethod.LLM
    assert by_item[("a", 1)].model == "test-model"
    assert by_item[("a", 2)].keyword == "agents"
    assert by_item[("b", 1)].keyword == "dbt"


def test_items_are_chunked_to_max_items_per_call() -> None:
    items = [_item(x) for x in ("a", "b", "c", "d", "e")]
    script = [
        _kw_text({"a": ["kw"], "b": ["kw"]}),
        _kw_text({"c": ["kw"], "d": ["kw"]}),
        _kw_text({"e": ["kw"]}),
    ]
    client, transport, _ = _client(script)
    result = run_extract(
        items,
        client,
        run_id="r",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
        max_items_per_call=2,
    )
    assert result.batches_total == 3
    assert result.batches_llm == 3
    assert len(transport.posts) == 3


def test_empty_input_makes_no_calls() -> None:
    client, transport, _ = _client([])
    result = run_extract([], client, run_id="r", clock=_CLOCK, fallback=FakeFallbackExtractor())
    assert result.batches_total == 0
    assert result.keywords == []
    assert transport.posts == []


# --- retry then fallback -----------------------------------------------------


def test_malformed_then_valid_on_retry_yields_llm_rows() -> None:
    client, transport, _ = _client(["not json", _kw_text({"a": ["genai"]})])
    result = run_extract(
        [_item("a")], client, run_id="r", clock=_CLOCK, fallback=FakeFallbackExtractor()
    )
    assert result.batches_llm == 1
    assert result.batches_fallback == 0
    assert result.llm_retries == 1
    assert len(transport.posts) == 2
    assert [k.keyword for k in result.keywords] == ["genai"]


def test_malformed_twice_falls_back_for_that_batch() -> None:
    fallback = FakeFallbackExtractor({"a": ["fb-one", "fb-two"]})
    client, transport, _ = _client(["not json", "still not json"])
    result = run_extract([_item("a")], client, run_id="r", clock=_CLOCK, fallback=fallback)

    assert result.batches_llm == 0
    assert result.batches_fallback == 1
    assert result.llm_retries == 1
    assert len(transport.posts) == 2
    assert fallback.calls == [["a"]]
    rows = sorted(result.keywords, key=lambda k: k.rank)
    assert [r.keyword for r in rows] == ["fb-one", "fb-two"]
    assert all(r.method is ExtractionMethod.FALLBACK for r in rows)
    assert all(r.model is None for r in rows)


def test_total_retries_are_bounded() -> None:
    # Two malformed batches, but only one retry allowed across the whole run:
    # batch a retries once (still bad -> fallback); batch b gets no retry.
    items = [_item("a"), _item("b")]
    client, transport, _ = _client(["bad", "bad", "bad"])
    result = run_extract(
        items,
        client,
        run_id="r",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
        max_items_per_call=1,
        max_malformed_retries=1,
    )
    assert result.llm_retries == 1
    assert result.batches_fallback == 2
    assert len(transport.posts) == 3  # a: 2 calls (1 retry), b: 1 call (no retry)


def test_network_llm_error_degrades_only_that_batch() -> None:
    # Batch a: transport fails every attempt -> LlmError -> fallback.
    # Batch b: valid LLM response.
    items = [_item("a"), _item("b")]
    fail = HttpResponse(status_code=503, content=b"", headers={})
    script: list[str | HttpResponse | Exception] = [
        fail,
        fail,
        fail,  # batch a: 3 attempts within one logical call, all 503
        _kw_text({"b": ["genai"]}),  # batch b succeeds
    ]
    client, _transport, _ = _client(script)
    result = run_extract(
        items,
        client,
        run_id="r",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
        max_items_per_call=1,
    )
    assert result.batches_fallback == 1
    assert result.batches_llm == 1
    assert result.budget_exhausted is False
    assert {k.item_id: k.method for k in result.keywords} == {
        "a": ExtractionMethod.FALLBACK,
        "b": ExtractionMethod.LLM,
    }


def test_budget_refused_retry_does_not_inflate_retry_metric() -> None:
    # First call is malformed; the one retry is refused by the budget breaker
    # (cap of 1) before any network I/O — so it must not count as a retry.
    client, transport, _ = _client(["not json"], max_calls=1)
    result = run_extract(
        [_item("a")], client, run_id="r", clock=_CLOCK, fallback=FakeFallbackExtractor()
    )
    assert result.llm_retries == 0
    assert result.batches_fallback == 1
    assert result.budget_exhausted is True
    assert len(transport.posts) == 1  # the refused retry sent nothing


# --- budget cascade ----------------------------------------------------------


def test_budget_exhaustion_sends_all_remaining_batches_to_fallback() -> None:
    items = [_item("a"), _item("b"), _item("c")]
    # Cap of 1: batch a uses the only call; batches b and c never touch the LLM.
    client, transport, logs = _client([_kw_text({"a": ["genai"]})], max_calls=1)
    result = run_extract(
        items,
        client,
        run_id="r",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
        max_items_per_call=1,
    )
    assert result.batches_llm == 1
    assert result.batches_fallback == 2
    assert result.budget_exhausted is True
    assert len(transport.posts) == 1  # b and c sent nothing over the wire
    assert len(logs) == 1  # only the one real call is logged


# --- fallback hygiene --------------------------------------------------------


def test_fallback_rows_are_truncated_to_max_keywords() -> None:
    fallback = FakeFallbackExtractor({"a": [f"kw{i}" for i in range(12)]})
    client, _, _ = _client(["bad", "bad"])
    result = run_extract(
        [_item("a")], client, run_id="r", clock=_CLOCK, fallback=fallback, max_keywords=3
    )
    assert [k.keyword for k in result.keywords] == ["kw0", "kw1", "kw2"]


def test_llm_log_written_once_per_real_call() -> None:
    # One malformed + one retry = two real calls = two llm_log rows.
    client, _, logs = _client(["bad", _kw_text({"a": ["genai"]})])
    run_extract([_item("a")], client, run_id="r", clock=_CLOCK, fallback=FakeFallbackExtractor())
    assert [e.status for e in logs] == ["ok", "ok"]  # both calls reached the transport
    assert all(e.purpose == "extract" and e.input_items == 1 for e in logs)
