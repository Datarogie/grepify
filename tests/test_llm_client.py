"""GRP-20: LLM provider - budget breaker, bounded+jittered retries, llm_log.

The budget circuit breaker is the load-bearing safety code (PRD §5, CSR
retry-loop lesson); the headline test is that the 41st call at a cap of 40 is
refused *without a network call*. No test makes a real network call - a scripted
in-memory transport stands in (PRD §9/§10, offline-testable by design).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from grepify.clock import FixedClock
from grepify.config.schemas import LlmProfile
from grepify.errors import BudgetExceededError, LlmError
from grepify.ingest.http import HttpResponse
from grepify.llm import ChatMessage, LlmClient, build_client
from grepify.llm.client import RetryPolicy
from grepify.models import LlmLogEntry
from tests.conftest import ScriptedCompletionTransport, envelope_response

_CLOCK = FixedClock(datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC))
_MESSAGES = [ChatMessage(role="user", content="hi")]


def _build(
    transport: ScriptedCompletionTransport,
    *,
    max_calls: int | None = None,
    max_attempts: int = 3,
    rng: Callable[[], float] = lambda: 0.0,
) -> tuple[LlmClient, list[LlmLogEntry], list[float]]:
    logs: list[LlmLogEntry] = []
    sleeps: list[float] = []
    client = LlmClient(
        model="test-model",
        base_url="https://api.example/v1",
        api_key="sk-test",
        log_sink=logs.append,
        clock=_CLOCK,
        transport=transport,
        max_calls_per_run=max_calls,
        retry=RetryPolicy(max_attempts=max_attempts, sleep=sleeps.append, rng=rng),
    )
    return client, logs, sleeps


def _call(client: LlmClient) -> object:
    return client.complete(_MESSAGES, run_id="run-1", purpose="extract", input_items=3)


# --- budget circuit breaker (the AC) ----------------------------------------


def test_41st_call_at_cap_40_is_refused_without_a_network_call() -> None:
    transport = ScriptedCompletionTransport([envelope_response("ok") for _ in range(40)])
    client, logs, _ = _build(transport, max_calls=40)

    for _ in range(40):
        _call(client)

    with pytest.raises(BudgetExceededError):
        _call(client)

    assert len(transport.posts) == 40  # the 41st sent nothing over the wire
    assert client.calls_made == 40  # a refusal never consumes budget
    assert len(logs) == 40  # and it is not a logged call


def test_budget_none_means_no_cap() -> None:
    transport = ScriptedCompletionTransport([envelope_response("ok") for _ in range(5)])
    client, _, _ = _build(transport, max_calls=None)
    for _ in range(5):
        _call(client)
    assert client.calls_made == 5


def test_budget_from_profile_via_build_client() -> None:
    transport = ScriptedCompletionTransport([envelope_response("ok") for _ in range(2)])
    logs: list[LlmLogEntry] = []
    profile = LlmProfile(endpoint="openai-compat", model="m", max_calls_per_run=2)
    client = build_client(
        profile,
        api_key="sk",
        base_url="https://x/v1",
        log_sink=logs.append,
        clock=_CLOCK,
        transport=transport,
    )
    _call(client)
    _call(client)
    with pytest.raises(BudgetExceededError):
        _call(client)


# --- happy path + llm_log ----------------------------------------------------


def test_successful_call_returns_completion_and_logs_ok_with_tokens() -> None:
    transport = ScriptedCompletionTransport(
        [envelope_response("hello world", prompt_tokens=12, completion_tokens=4)]
    )
    client, logs, _ = _build(transport)

    completion = client.complete(_MESSAGES, run_id="run-9", purpose="extract", input_items=3)

    assert completion.text == "hello world"
    assert completion.tokens_in == 12
    assert completion.tokens_out == 4
    assert len(logs) == 1
    (entry,) = logs
    assert entry.status == "ok"
    assert entry.run_id == "run-9"
    assert entry.purpose == "extract"
    assert entry.model == "test-model"
    assert entry.input_items == 3
    assert entry.tokens_in == 12
    assert entry.tokens_out == 4
    assert entry.created_at == "2026-07-08T12:00:00+00:00"


def test_missing_usage_logs_ok_with_none_tokens() -> None:
    transport = ScriptedCompletionTransport(
        [envelope_response("hi", prompt_tokens=None, completion_tokens=None)]
    )
    client, logs, _ = _build(transport)
    completion = _call(client)
    assert completion.tokens_in is None  # type: ignore[attr-defined]
    assert logs[0].status == "ok"
    assert logs[0].tokens_in is None


# --- request shaping (headers/url/payload); credentials never logged ---------


def _client_with(
    transport: ScriptedCompletionTransport, *, base_url: str, api_key: str | None
) -> LlmClient:
    return LlmClient(
        model="test-model",
        base_url=base_url,
        api_key=api_key,
        log_sink=lambda _e: None,
        clock=_CLOCK,
        transport=transport,
    )


def test_request_targets_chat_completions_with_auth_and_deterministic_payload() -> None:
    transport = ScriptedCompletionTransport([envelope_response("ok")])
    # Trailing slash on the base URL is tolerated.
    client = _client_with(transport, base_url="https://api.example/v1/", api_key="sk-test")
    _call(client)

    url, headers, payload = transport.posts[0]
    assert url == "https://api.example/v1/chat/completions"
    assert headers["authorization"] == "Bearer sk-test"
    assert headers["content-type"] == "application/json"
    assert payload["model"] == "test-model"
    assert payload["temperature"] == 0
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_no_api_key_omits_authorization_header() -> None:
    transport = ScriptedCompletionTransport([envelope_response("ok")])
    client = _client_with(transport, base_url="https://api.example/v1", api_key=None)
    _call(client)
    _url, headers, _payload = transport.posts[0]
    assert "authorization" not in headers


# --- retries (bounded + jittered) -------------------------------------------


def test_retries_on_500_then_succeeds() -> None:
    transport = ScriptedCompletionTransport(
        [HttpResponse(status_code=500, content=b"", headers={}), envelope_response("ok")]
    )
    client, logs, sleeps = _build(transport, max_attempts=3)
    _call(client)
    assert len(transport.posts) == 2
    assert sleeps == [1.0]  # exponential backoff, rng=0
    assert client.calls_made == 1  # one logical call regardless of retries
    assert len(logs) == 1 and logs[0].status == "ok"


def test_retries_exhausted_raises_llm_error_and_logs_one_error() -> None:
    transport = ScriptedCompletionTransport(
        [HttpResponse(status_code=503, content=b"", headers={}) for _ in range(3)]
    )
    client, logs, sleeps = _build(transport, max_attempts=3)
    with pytest.raises(LlmError):
        _call(client)
    assert len(transport.posts) == 3
    assert sleeps == [1.0, 2.0]  # no sleep after the final attempt
    assert len(logs) == 1 and logs[0].status == "error"
    assert logs[0].tokens_in is None


def test_jitter_is_added_when_rng_is_positive() -> None:
    transport = ScriptedCompletionTransport(
        [HttpResponse(status_code=500, content=b"", headers={}), envelope_response("ok")]
    )
    client, _, sleeps = _build(transport, max_attempts=3, rng=lambda: 1.0)
    _call(client)
    assert sleeps == [2.0]  # backoff(0) = 1.0 (exp) + 1.0 (jitter*base)


def test_transport_exception_is_retryable() -> None:
    transport = ScriptedCompletionTransport(
        [LlmError("connection refused"), envelope_response("ok")]
    )
    client, _, _ = _build(transport, max_attempts=2)
    _call(client)
    assert len(transport.posts) == 2


def test_non_retryable_4xx_short_circuits_without_retry() -> None:
    transport = ScriptedCompletionTransport(
        [HttpResponse(status_code=400, content=b"bad request", headers={})]
    )
    client, logs, sleeps = _build(transport, max_attempts=3)
    with pytest.raises(LlmError, match="non-retryable"):
        _call(client)
    assert len(transport.posts) == 1  # no wasted retries on a hard client error
    assert sleeps == []
    assert logs[0].status == "error"


def test_malformed_success_envelope_raises_llm_error_and_does_not_retry() -> None:
    transport = ScriptedCompletionTransport(
        [HttpResponse(status_code=200, content=b"not json at all", headers={})]
    )
    client, logs, _ = _build(transport, max_attempts=3)
    with pytest.raises(LlmError, match="malformed"):
        _call(client)
    assert len(transport.posts) == 1
    assert logs[0].status == "error"


# --- build_client guards -----------------------------------------------------


def test_build_client_rejects_non_openai_endpoint() -> None:
    profile = LlmProfile(endpoint="anthropic", model="claude-haiku-4-5", max_calls_per_run=40)
    with pytest.raises(LlmError, match="unsupported llm endpoint"):
        build_client(
            profile, api_key="k", base_url="https://x", log_sink=lambda _e: None, clock=_CLOCK
        )


def test_build_client_requires_model() -> None:
    profile = LlmProfile(endpoint="openai-compat", model=None, max_calls_per_run=40)
    with pytest.raises(LlmError, match="requires a 'model'"):
        build_client(
            profile, api_key="k", base_url="https://x", log_sink=lambda _e: None, clock=_CLOCK
        )


def test_zero_max_attempts_is_rejected() -> None:
    with pytest.raises(LlmError, match="max_attempts"):
        LlmClient(
            model="m",
            base_url="https://x/v1",
            api_key=None,
            log_sink=lambda _e: None,
            clock=_CLOCK,
            retry=RetryPolicy(max_attempts=0),
        )
