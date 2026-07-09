"""LLM provider: OpenAI-compat client, budget breaker, bounded retries (GRP-20).

This is the *only* module that talks to a model. Everything risky about calling
an LLM is contained here so the rest of the pipeline can treat extraction (and,
later, digests — PRD §6 ``llm_log.purpose``) as an ordinary, budgeted operation:

- **Budget circuit breaker (PRD §5, the CSR retry-loop lesson).** A hard,
  per-run ceiling of ``max_calls_per_run`` *logical* calls. The breaker is
  checked before any network I/O; the call over the cap is refused with
  :class:`~grepify.errors.BudgetExceededError` — no request is sent, ever. This is a
  hard stop, never an unbounded loop.
- **Bounded, jittered retries.** Within one logical call, transient failures
  (transport error, HTTP 429/5xx) are retried at most ``max_attempts`` times
  with exponential backoff + jitter. A non-retryable 4xx short-circuits. The
  ``sleep`` and ``rng`` collaborators are injected so tests are deterministic
  and never actually sleep.
- **``llm_log`` for every real call (PRD §6).** Exactly one
  :class:`~grepify.models.LlmLogEntry` is written per logical call that reaches
  the transport — ``status='ok'`` with token usage on success, ``status='error'``
  when retries are exhausted or the envelope is malformed. A budget refusal
  writes no row (it is not a call).

Only ``endpoint='openai-compat'`` is implemented (GRP-20 scope); other endpoints
(anthropic/vertex/cli, PRD §5) are later work and :func:`build_client` rejects
them. The endpoint base URL and API key are deployment secrets injected by the
caller (GRP-25 resolves them from the environment); they are stored privately
and never logged (PRD §5 security — no credential-bearing config in logs).

Failure modes
-------------
- Over budget → :class:`~grepify.errors.BudgetExceededError` (before any network I/O).
- Transport failure / retryable HTTP status, retries exhausted → non-fatal
  :class:`~grepify.errors.LlmError` (the batcher, GRP-21, degrades to fallback).
- Non-retryable HTTP status (most 4xx) or a malformed/unparseable success
  envelope → :class:`~grepify.errors.LlmError` immediately (no wasted retries).
- Unsupported endpoint / missing model at construction → :class:`~grepify.errors.LlmError`.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from grepify.clock import Clock, to_iso
from grepify.config.schemas import LlmProfile
from grepify.errors import BudgetExceededError, LlmError
from grepify.llm.transport import CompletionTransport, HttpxCompletionTransport
from grepify.models import LlmLogEntry

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

LogSink = Callable[[LlmLogEntry], None]


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded-retry + timing knobs for :class:`LlmClient` (PRD §5).

    ``sleep`` and ``rng`` are injected so tests are deterministic and never
    actually sleep; the defaults are the production values.
    """

    max_attempts: int = 3
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 30.0
    timeout_seconds: float = 30.0
    sleep: Callable[[float], None] = time.sleep
    rng: Callable[[], float] = random.random


# Shared immutable default (a frozen dataclass is safe to share); avoids calling
# RetryPolicy() in argument defaults (ruff B008).
_DEFAULT_RETRY = RetryPolicy()


@dataclass(frozen=True)
class ChatMessage:
    """One OpenAI-compat chat message."""

    role: str
    content: str


@dataclass(frozen=True)
class Completion:
    """A model's reply plus token accounting (``None`` if the provider omits usage)."""

    text: str
    tokens_in: int | None
    tokens_out: int | None


class _BudgetGate:
    """The per-run budget circuit breaker (PRD §5).

    ``reserve`` is called once per logical call *before* any network I/O: it
    raises :class:`~grepify.errors.BudgetExceededError` when the cap is already
    reached (without incrementing, so a refusal never consumes budget), and
    otherwise records the call. ``max_calls=None`` disables the cap.
    """

    def __init__(self, max_calls: int | None) -> None:
        self._max_calls = max_calls
        self.calls_made = 0

    def reserve(self) -> None:
        if self._max_calls is not None and self.calls_made >= self._max_calls:
            raise BudgetExceededError(
                f"llm budget exhausted: {self.calls_made}/{self._max_calls} calls this run"
            )
        self.calls_made += 1


class LlmClient:
    """OpenAI-compatible chat-completions client with a budget breaker + retries.

    Construct via :func:`build_client` in production; the explicit constructor is
    for tests that inject a canned :class:`~grepify.llm.transport.CompletionTransport`.
    """

    def __init__(  # noqa: PLR0913 — a config-heavy but flat client seam; knobs bundle into RetryPolicy
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        log_sink: LogSink,
        clock: Clock,
        transport: CompletionTransport | None = None,
        max_calls_per_run: int | None = None,
        retry: RetryPolicy = _DEFAULT_RETRY,
    ) -> None:
        if retry.max_attempts < 1:
            raise LlmError("retry.max_attempts must be >= 1")
        self._model = model
        # Trailing slash tolerated so callers need not care; the API key is held
        # privately and only ever placed in the Authorization header (never logged).
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._log_sink = log_sink
        self._clock = clock
        self._transport = transport or HttpxCompletionTransport()
        self._gate = _BudgetGate(max_calls_per_run)
        self._max_calls_per_run = max_calls_per_run
        self._retry = retry

    @property
    def model(self) -> str:
        return self._model

    @property
    def calls_made(self) -> int:
        """Logical calls that have passed the budget gate this run (budget usage)."""
        return self._gate.calls_made

    @property
    def max_calls_per_run(self) -> int | None:
        return self._max_calls_per_run

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        run_id: str,
        purpose: str,
        input_items: int,
    ) -> Completion:
        """Run one chat completion, subject to the budget breaker.

        See the module docstring for the contract. ``purpose`` and ``input_items``
        are recorded on the :class:`~grepify.models.LlmLogEntry` for auditability
        (PRD §6). Raises :class:`~grepify.errors.BudgetExceededError` before any
        network I/O when over budget, or :class:`~grepify.errors.LlmError` when
        the call cannot be completed after bounded retries.
        """
        # Reserve budget BEFORE any network I/O: the call over the cap sends no
        # request. A refusal raises here and writes no llm_log row (not a call).
        self._gate.reserve()
        created_at = to_iso(self._clock.now())

        def entry(status: str, completion: Completion | None) -> LlmLogEntry:
            return LlmLogEntry(
                run_id=run_id,
                purpose=purpose,
                model=self._model,
                input_items=input_items,
                tokens_in=completion.tokens_in if completion else None,
                tokens_out=completion.tokens_out if completion else None,
                status=status,
                created_at=created_at,
            )

        try:
            completion = self._attempt_with_retries(messages)
        except LlmError:
            # Every real call is logged, including failures (PRD §6).
            self._log_sink(entry("error", None))
            raise
        self._log_sink(entry("ok", completion))
        return completion

    # --- internals -----------------------------------------------------------

    def _attempt_with_retries(self, messages: Sequence[ChatMessage]) -> Completion:
        payload = self._build_payload(messages)
        headers = self._headers()
        last_error = "no attempts made"
        for attempt in range(self._retry.max_attempts):
            try:
                response = self._transport.post_json(
                    self._url, headers=headers, payload=payload, timeout=self._retry.timeout_seconds
                )
            except LlmError as exc:
                last_error = str(exc)  # transport-level failure — retryable
            else:
                if 200 <= response.status_code < 300:
                    return self._parse_completion(response.content)
                if response.status_code not in _RETRYABLE_STATUSES:
                    raise LlmError(f"llm returned non-retryable http {response.status_code}")
                last_error = f"llm returned http {response.status_code}"
            if attempt < self._retry.max_attempts - 1:
                self._retry.sleep(self._backoff(attempt))
        raise LlmError(f"llm call failed after {self._retry.max_attempts} attempts: {last_error}")

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with additive jitter (both bounded).

        ``rng`` is injected, so a test passing ``rng=lambda: 0.0`` observes clean
        exponential delays and can assert them exactly.
        """
        capped = min(
            self._retry.backoff_cap_seconds, self._retry.backoff_base_seconds * (2.0**attempt)
        )
        return capped + self._retry.rng() * self._retry.backoff_base_seconds

    def _build_payload(self, messages: Sequence[ChatMessage]) -> dict[str, Any]:
        return {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            # Deterministic decoding: extraction is a structured task, not creative.
            "temperature": 0,
        }

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    def _parse_completion(self, content: bytes) -> Completion:
        try:
            data = json.loads(content)
            text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise TypeError("choices[0].message.content is not a string")
            usage = data.get("usage") or {}
            return Completion(
                text=text,
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
            )
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"malformed openai-compat response envelope: {exc}") from exc


def build_client(  # noqa: PLR0913 — thin factory forwarding config + injected deps to LlmClient
    profile: LlmProfile,
    *,
    api_key: str | None,
    base_url: str,
    log_sink: LogSink,
    clock: Clock,
    transport: CompletionTransport | None = None,
    retry: RetryPolicy = _DEFAULT_RETRY,
) -> LlmClient:
    """Build an :class:`LlmClient` from a config :class:`~grepify.config.schemas.LlmProfile`.

    ``base_url`` and ``api_key`` are deployment secrets supplied by the caller
    (never read from committed config, PRD §5). Raises
    :class:`~grepify.errors.LlmError` for a non-``openai-compat`` endpoint or a
    profile missing its model — both out of scope for GRP-20.
    """
    if profile.endpoint != "openai-compat":
        raise LlmError(
            f"unsupported llm endpoint {profile.endpoint!r}; only 'openai-compat' "
            "is implemented (GRP-20)"
        )
    if profile.model is None:
        raise LlmError("openai-compat profile requires a 'model'")
    return LlmClient(
        model=profile.model,
        base_url=base_url,
        api_key=api_key,
        log_sink=log_sink,
        clock=clock,
        transport=transport,
        max_calls_per_run=profile.max_calls_per_run,
        retry=retry,
    )
