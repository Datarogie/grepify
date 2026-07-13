"""Shared HTTP transport for the concrete fetchers (GRP-11/12/13).

Fetchers depend on the small :class:`Transport` protocol, not on ``httpx``
directly, so tests inject a canned in-memory transport and never touch the
network (PRD §9 - fetchers are fully unit-testable without network). The real
implementation (:class:`HttpxTransport`) is what production wires in.

Failure modes
-------------
:meth:`Transport.get` never raises for an HTTP error status (4xx/5xx) - it
returns an :class:`HttpResponse` carrying that status and lets the caller
decide (RSS treats 304 as "unchanged", Reddit retries 429/5xx before giving
up, per §8 F-ING-01/F-ING-04). It DOES raise :class:`~grepify.errors.FetchError`
for failures that never produced a response at all: connection refused, DNS
failure, TLS error, or the request exceeding its timeout. :func:`get_or_raise`
is the one place that translates such a transport exception into a
per-source-scoped :class:`~grepify.errors.FetchError`, so all three fetchers
isolate failures identically (PRD §9 - one dead source never fails the run).

TLS posture
-----------
:class:`HttpxTransport` fetches through an :class:`ssl.SSLContext` pinned to
security level 1 (``DEFAULT@SECLEVEL=1``) so feeds hosted on older or
misconfigured servers - the ones whose legacy ciphers/keys OpenSSL 3 rejects at
its default security level 2, surfacing as an "sslv3 alert handshake failure" -
can still complete their handshake. Certificate verification stays fully ON
(``check_hostname`` and ``verify_mode`` keep their secure defaults) and the
protocol floor stays at TLS 1.2; only weaker ciphers/keys are permitted, never
weaker protocol versions. The context only PERMITS weaker crypto, it never
forces it, so strong servers still negotiate strong crypto. Public feeds carry
no secrets or auth, so accepting legacy ciphers is the standard feed-reader
posture.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Protocol

import httpx

from grepify.errors import FetchError


def _build_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that permits legacy-server ciphers (security level
    1) while keeping certificate verification ON. See the module docstring's
    "TLS posture" note for why. The protocol floor stays at TLS 1.2.
    """
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


@dataclass(frozen=True)
class HttpResponse:
    """A transport-neutral HTTP response (status, body, headers)."""

    status_code: int
    content: bytes
    headers: dict[str, str]  # lowercase keys (etag, last-modified, ...)


class Transport(Protocol):
    """What a fetcher needs from an HTTP client. See module docstring."""

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse: ...


class HttpxTransport:
    """Production :class:`Transport`, backed by ``httpx``.

    Uses a security-level-1 SSL context (see module "TLS posture") so legacy
    feed servers negotiate; cert verification stays on. The context can be
    injected for testing.
    """

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context or _build_ssl_context()

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
        try:
            response = httpx.get(
                url,
                headers=headers,
                timeout=timeout,
                follow_redirects=True,
                verify=self._ssl_context,
            )
        except httpx.HTTPError as exc:
            raise FetchError(f"GET {url} failed: {exc}") from exc
        return HttpResponse(
            status_code=response.status_code,
            content=response.content,
            headers={key.lower(): value for key, value in response.headers.items()},
        )


def get_or_raise(
    transport: Transport, url: str, *, headers: dict[str, str], timeout: float, source_id: str
) -> HttpResponse:
    """GET ``url``, translating any transport-level exception into a
    :class:`~grepify.errors.FetchError` scoped to ``source_id`` (per-source
    isolation, PRD §9). Does not interpret the status code - callers decide
    what counts as success for their protocol.
    """
    try:
        return transport.get(url, headers=headers, timeout=timeout)
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"{source_id}: fetch failed: {exc}") from exc
