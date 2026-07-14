"""HTTP transport for the LLM provider (GRP-20).

The provider depends on the small :class:`CompletionTransport` protocol (a JSON
POST), not on ``httpx`` directly, so tests inject a canned in-memory transport
and the module never touches the network - GRP-20/21 are offline-testable by
design (PRD §9/§10). :class:`HttpxCompletionTransport` is what production wires
in (GRP-25). The transport-neutral :class:`~grepify.ingest.http.HttpResponse` is
reused so there is one response shape across the codebase.

Failure modes
-------------
:meth:`CompletionTransport.post_json` never raises for an HTTP error status
(4xx/5xx) - it returns the response and lets :class:`~grepify.llm.client.LlmClient`
decide (retry 429/5xx, short-circuit other 4xx). It DOES raise
:class:`~grepify.errors.LlmError` for a failure that produced no response at all
(connection refused, DNS/TLS failure, timeout); the client treats that as a
retryable transport failure. Request headers (which carry the API key) are never
logged here or anywhere (PRD §5 security).
"""

from __future__ import annotations

from typing import Any, Protocol

from grepify.errors import LlmError
from grepify.ingest.http import (
    HttpResponse,
    OutboundHttpClient,
    OutboundPolicy,
    OutboundRequestError,
)


class CompletionTransport(Protocol):
    """What :class:`~grepify.llm.client.LlmClient` needs from an HTTP client."""

    def post_json(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any], timeout: float
    ) -> HttpResponse: ...


class HttpxCompletionTransport:
    """Production :class:`CompletionTransport`, backed by the central outbound policy."""

    def __init__(self, *, client: OutboundHttpClient | None = None) -> None:
        self._client = client or OutboundHttpClient(policy=OutboundPolicy(max_redirects=0))

    def post_json(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any], timeout: float
    ) -> HttpResponse:
        try:
            return self._client.post_json(url, headers=headers, payload=payload, timeout=timeout)
        except OutboundRequestError as exc:
            # Never interpolate `headers` (Authorization/API key) into the message.
            raise LlmError(f"POST failed: {exc}") from exc
