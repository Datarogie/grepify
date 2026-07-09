"""LLM provider (E2, GRP-20): the one module that talks to a model.

Public surface the extract batcher (GRP-21) and, later, the digest generator
(E4) build on:

- :class:`LlmClient` — OpenAI-compat chat completions wrapped in a per-run
  budget circuit breaker and bounded, jittered retries; writes an ``llm_log``
  row for every real call (PRD §5/§6).
- :func:`build_client` — construct a client from a config
  :class:`~grepify.config.schemas.LlmProfile` plus injected deployment secrets.
- :class:`ChatMessage` / :class:`Completion` — the request/response value types.
- :class:`CompletionTransport` — the injectable HTTP seam (offline tests inject
  a canned transport; production uses :class:`HttpxCompletionTransport`).

Budget refusal (:class:`~grepify.errors.BudgetExceededError`) and call failure
(:class:`~grepify.errors.LlmError`) live in :mod:`grepify.errors`.

Failure modes
-------------
None of its own — this is a re-export aggregator. See :mod:`grepify.llm.client`
and :mod:`grepify.llm.transport` for the module-level failure modes.
"""

from __future__ import annotations

from grepify.llm.client import ChatMessage, Completion, LlmClient, LogSink, build_client
from grepify.llm.transport import (
    CompletionTransport,
    HttpxCompletionTransport,
)

__all__ = [
    "ChatMessage",
    "Completion",
    "CompletionTransport",
    "HttpxCompletionTransport",
    "LlmClient",
    "LogSink",
    "build_client",
]
