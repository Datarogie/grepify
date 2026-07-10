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

import httpx

from grepify.errors import LlmError
from grepify.ingest.http import HttpResponse


class CompletionTransport(Protocol):
    """What :class:`~grepify.llm.client.LlmClient` needs from an HTTP client."""

    def post_json(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any], timeout: float
    ) -> HttpResponse: ...


class HttpxCompletionTransport:
    """Production :class:`CompletionTransport`, backed by ``httpx``."""

    def post_json(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any], timeout: float
    ) -> HttpResponse:
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            # Never interpolate `headers` (Authorization/API key) into the message.
            raise LlmError(f"POST {url} failed: {exc}") from exc
        return HttpResponse(
            status_code=response.status_code,
            content=response.content,
            headers={key.lower(): value for key, value in response.headers.items()},
        )
