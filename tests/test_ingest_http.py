"""#45: HttpxTransport TLS wiring (security-level-1 SSL context).

Covers only the SSL-context construction and that HttpxTransport threads that
context through to ``httpx.get`` via ``verify=``. Network-free and deterministic:
the fetcher tests inject a fake transport and never exercise HttpxTransport, so
this is the one place the real transport's wiring is checked.
"""

from __future__ import annotations

import ssl
from typing import Any, ClassVar

import httpx

from grepify.ingest.http import HttpxTransport, _build_ssl_context


def test_build_ssl_context_permits_legacy_ciphers_but_keeps_verification() -> None:
    ctx = _build_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    # Protocol floor stays modern; only ciphers/keys are relaxed.
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    # Certificate verification is preserved.
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_transport_uses_the_context_by_default() -> None:
    transport = HttpxTransport()
    ctx = transport._ssl_context
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


class _StubResponse:
    """Minimal stand-in for what HttpxTransport.get reads off httpx's response."""

    status_code = 200
    content = b"<rss/>"
    headers: ClassVar[dict[str, str]] = {"ETag": "abc"}


def test_get_passes_ssl_context_as_verify(monkeypatch: Any) -> None:
    recorded: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _StubResponse:
        recorded["url"] = url
        recorded.update(kwargs)
        return _StubResponse()

    monkeypatch.setattr(httpx, "get", fake_get)

    transport = HttpxTransport()
    response = transport.get("https://example.com/feed", headers={}, timeout=5.0)

    # The transport's own context is handed to httpx as verify=.
    assert recorded["verify"] is transport._ssl_context
    assert recorded["follow_redirects"] is True
    # And the response is mapped through with lowercased header keys.
    assert response.status_code == 200
    assert response.content == b"<rss/>"
    assert response.headers == {"etag": "abc"}


def test_injected_context_is_used() -> None:
    injected = ssl.create_default_context()
    transport = HttpxTransport(ssl_context=injected)
    assert transport._ssl_context is injected
