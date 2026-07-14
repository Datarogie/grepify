from __future__ import annotations

import ipaddress
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from grepify.errors import FetchError, LlmError
from grepify.ingest import http as http_mod
from grepify.ingest.http import (
    HttpCompatibility,
    HttpxTransport,
    OutboundErrorKind,
    OutboundHttpClient,
    OutboundPolicy,
    OutboundRequestError,
    _PolicyNetworkBackend,
    get_or_raise,
)
from grepify.llm.transport import HttpxCompletionTransport


def resolver(
    *addresses: str,
) -> Callable[[str, int], list[ipaddress.IPv4Address | ipaddress.IPv6Address]]:
    def resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return [ipaddress.ip_address(address) for address in addresses]

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/feed",
        "https://[::1]/feed",
        "https://10.0.0.1/feed",
        "https://192.168.1.1/feed",
        "https://172.16.0.1/feed",
        "https://[fc00::1]/feed",
        "https://169.254.1.1/feed",
        "https://[fe80::1]/feed",
        "https://100.64.0.1/feed",
        "https://224.0.0.1/feed",
        "https://0.0.0.0/feed",
        "https://240.0.0.1/feed",
        "https://192.0.2.1/feed",
        "https://[::ffff:127.0.0.1]/feed",
    ],
)
def test_unsafe_literal_addresses_are_rejected(url: str) -> None:
    client = OutboundHttpClient(
        transport_factory=lambda _: httpx.MockTransport(lambda r: httpx.Response(200))
    )
    with pytest.raises(OutboundRequestError) as exc:
        client.get(url, headers={}, timeout=1)
    assert exc.value.kind is OutboundErrorKind.UNSAFE_DESTINATION


@pytest.mark.parametrize("url", ["https://8.8.8.8/feed", "https://[2606:4700:4700::1111]/feed"])
def test_public_literal_addresses_are_allowed(url: str) -> None:
    client = OutboundHttpClient(
        transport_factory=lambda _: httpx.MockTransport(lambda r: httpx.Response(200))
    )
    assert client.get(url, headers={}, timeout=1).status_code == 200


def test_dns_multiple_safe_addresses_allowed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"ok")

    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8", "1.1.1.1"),
        transport_factory=lambda _: httpx.MockTransport(handler),
    )
    assert client.get("https://example.com/feed", headers={}, timeout=1).content == b"ok"
    assert calls == ["https://example.com/feed"]


def test_dns_any_unsafe_address_fails_closed_before_request() -> None:
    sent: list[str] = []
    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8", "127.0.0.1"),
        transport_factory=lambda _: httpx.MockTransport(
            lambda r: sent.append(str(r.url)) or httpx.Response(200)
        ),
    )
    with pytest.raises(OutboundRequestError) as exc:
        client.get("https://example.com/feed", headers={}, timeout=1)
    assert exc.value.kind is OutboundErrorKind.UNSAFE_DESTINATION
    assert sent == []


@pytest.mark.parametrize(
    "url,kind",
    [
        ("http://example.com/feed", OutboundErrorKind.UNSUPPORTED_SCHEME),
        ("ftp://example.com/feed", OutboundErrorKind.UNSUPPORTED_SCHEME),
        ("//example.com/feed", OutboundErrorKind.UNSUPPORTED_SCHEME),
        ("https://user@example.com/feed", OutboundErrorKind.EMBEDDED_CREDENTIALS),
        ("https://user:pass@example.com/feed", OutboundErrorKind.EMBEDDED_CREDENTIALS),
        ("https:///feed", OutboundErrorKind.INVALID_HOST),
        ("https://example.com:bad/feed", OutboundErrorKind.INVALID_HOST),
        ("https://example.com:444/feed", OutboundErrorKind.INVALID_HOST),
        ("https://example.com/\x00feed", OutboundErrorKind.INVALID_HOST),
        ("https://0x7f000001/feed", OutboundErrorKind.INVALID_HOST),
        ("https://2130706433/feed", OutboundErrorKind.INVALID_HOST),
        ("https://0177.0.0.1/feed", OutboundErrorKind.INVALID_HOST),
        ("https://127.1/feed", OutboundErrorKind.INVALID_HOST),
        ("https://[fe80::1%25eth0]/feed", OutboundErrorKind.INVALID_HOST),
    ],
)
def test_url_policy_rejections(url: str, kind: OutboundErrorKind) -> None:
    client = OutboundHttpClient(
        transport_factory=lambda _: httpx.MockTransport(lambda r: httpx.Response(200))
    )
    with pytest.raises(OutboundRequestError) as exc:
        client.get(url, headers={}, timeout=1)
    assert exc.value.kind is kind


def test_http_can_be_enabled_for_exact_normalized_host() -> None:
    client = OutboundHttpClient(
        policy=OutboundPolicy(http=HttpCompatibility(frozenset({"example.com"}))),
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    assert client.get("http://EXAMPLE.com./feed", headers={}, timeout=1).status_code == 200


def test_internationalized_hostname_uses_idna_for_resolution() -> None:
    resolved: list[str] = []

    def fake_resolver(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        resolved.append(host)
        return [ipaddress.ip_address("8.8.8.8")]

    client = OutboundHttpClient(
        resolver=fake_resolver,
        transport_factory=lambda _: httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    assert client.get("https://bücher.example/feed", headers={}, timeout=1).status_code == 200
    assert resolved == ["xn--bcher-kva.example"]


def test_redirects_are_validated_before_next_request() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})

    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(handler),
    )
    with pytest.raises(OutboundRequestError) as exc:
        client.get("https://example.com/feed", headers={}, timeout=1)
    assert exc.value.kind is OutboundErrorKind.UNSAFE_DESTINATION
    assert sent == ["https://example.com/feed"]


def test_legitimate_relative_and_cross_origin_redirects() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        if str(request.url) == "https://example.com/feed":
            return httpx.Response(302, headers={"location": "/rss"})
        if str(request.url) == "https://example.com/rss":
            return httpx.Response(302, headers={"location": "https://other.example/feed"})
        return httpx.Response(200, content=b"ok")

    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(handler),
    )
    assert client.get("https://example.com/feed", headers={}, timeout=1).content == b"ok"
    assert sent == [
        "https://example.com/feed",
        "https://example.com/rss",
        "https://other.example/feed",
    ]


@pytest.mark.parametrize(
    "location,kind",
    [
        ("ftp://example.com/feed", OutboundErrorKind.UNSUPPORTED_SCHEME),
        ("http://example.com/feed", OutboundErrorKind.UNSUPPORTED_SCHEME),
        ("https://example.com/feed", OutboundErrorKind.REDIRECT_LOOP),
    ],
)
def test_redirect_rejections(location: str, kind: OutboundErrorKind) -> None:
    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"location": location})
        ),
    )
    with pytest.raises(OutboundRequestError) as exc:
        client.get("https://example.com/feed", headers={}, timeout=1)
    assert exc.value.kind is kind


def test_redirect_limit_and_missing_location() -> None:
    client = OutboundHttpClient(
        policy=OutboundPolicy(max_redirects=0),
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"location": "/next"})
        ),
    )
    with pytest.raises(OutboundRequestError) as exc:
        client.get("https://example.com/feed", headers={}, timeout=1)
    assert exc.value.kind is OutboundErrorKind.REDIRECT_LIMIT

    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(lambda r: httpx.Response(302)),
    )
    with pytest.raises(OutboundRequestError) as exc2:
        client.get("https://example.com/feed", headers={}, timeout=1)
    assert exc2.value.kind is OutboundErrorKind.UNSAFE_REDIRECT


def test_sensitive_headers_are_stripped_on_cross_origin_redirect() -> None:
    headers_seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers_seen.append({k.lower(): v for k, v in request.headers.items()})
        if str(request.url) == "https://example.com/feed":
            return httpx.Response(302, headers={"location": "https://other.example/feed"})
        return httpx.Response(200)

    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(handler),
    )
    client.get(
        "https://example.com/feed",
        headers={
            "Authorization": "Bearer secret",
            "Proxy-Authorization": "Basic secret",
            "Cookie": "a=b",
            "X-Keep": "1",
            "Host": "evil",
        },
        timeout=1,
    )
    assert "authorization" in headers_seen[0]
    assert "proxy-authorization" not in headers_seen[0]
    assert "cookie" in headers_seen[0]
    assert "host" not in headers_seen[0] or headers_seen[0]["host"] == "example.com"
    assert "authorization" not in headers_seen[1]
    assert "proxy-authorization" not in headers_seen[1]
    assert "cookie" not in headers_seen[1]
    assert headers_seen[1]["x-keep"] == "1"


def test_same_origin_redirect_retains_safe_headers() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({k.lower(): v for k, v in request.headers.items()})
        return (
            httpx.Response(302, headers={"location": "/next"})
            if len(seen) == 1
            else httpx.Response(200)
        )

    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(handler),
    )
    client.get("https://example.com/feed", headers={"Authorization": "Bearer safe"}, timeout=1)
    assert seen[1]["authorization"] == "Bearer safe"


def test_errors_redact_sensitive_query_values() -> None:
    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("boom"))
        ),
    )
    with pytest.raises(OutboundRequestError) as exc:
        client.get("https://example.com/feed?token=secret&ok=visible", headers={}, timeout=1)
    text = str(exc.value)
    assert "secret" not in text
    assert "token=REDACTED" in text
    assert "ok=visible" in text


def test_bound_network_backend_uses_validated_addresses_without_second_dns(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []

    class DummyBackend:
        def connect_tcp(self, host: str, port: int, *args: Any) -> object:
            calls.append(host)
            return object()

    bound = http_mod._BoundResolver(resolver("8.8.8.8"))
    bound.validate("example.com", 443)
    backend = _PolicyNetworkBackend(bound)
    monkeypatch.setattr(backend, "_backend", DummyBackend())
    backend.connect_tcp("example.com", 443)
    assert calls == ["8.8.8.8"]


def test_httpx_transport_uses_central_client() -> None:
    transport = HttpxTransport(
        client=OutboundHttpClient(
            policy=OutboundPolicy(max_redirects=0),
            resolver=resolver("8.8.8.8"),
            transport_factory=lambda _: httpx.MockTransport(
                lambda r: httpx.Response(200, headers={"ETag": "abc"}, content=b"ok")
            ),
        )
    )
    response = transport.get("https://example.com/feed", headers={}, timeout=1)
    assert response.status_code == 200
    assert response.headers["etag"] == "abc"


def test_get_or_raise_redacts_generic_exceptions() -> None:
    class BadTransport:
        def get(self, url: str, *, headers: dict[str, str], timeout: float) -> Any:
            raise RuntimeError("https://example.com/feed?token=secret")

    with pytest.raises(FetchError) as exc:
        get_or_raise(
            BadTransport(),
            "https://example.com/feed?token=secret",
            headers={},
            timeout=1,
            source_id="s",
        )
    assert "secret" not in str(exc.value)


def test_no_direct_httpx_module_calls_outside_policy() -> None:
    offenders: list[str] = []
    for path in Path("grepify").rglob("*.py"):
        if path.as_posix() == "grepify/ingest/http.py":
            continue
        text = path.read_text()
        for needle in (
            "httpx.get",
            "httpx.post",
            "httpx.request",
            "httpx.Client",
            "httpx.AsyncClient",
        ):
            if needle in text:
                offenders.append(f"{path}:{needle}")
    assert offenders == []


def test_redirected_hostname_dns_is_validated_before_second_request() -> None:
    sent: list[str] = []
    answers = {
        "example.com": [ipaddress.ip_address("8.8.8.8")],
        "next.example": [ipaddress.ip_address("10.0.0.1")],
    }

    def fake_resolver(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return answers[host]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://next.example/feed"})

    client = OutboundHttpClient(
        resolver=fake_resolver,
        transport_factory=lambda _: httpx.MockTransport(handler),
    )
    with pytest.raises(OutboundRequestError) as exc:
        client.get("https://example.com/feed", headers={}, timeout=1)
    assert exc.value.kind is OutboundErrorKind.UNSAFE_DESTINATION
    assert sent == ["https://example.com/feed"]


def test_llm_transport_uses_policy_and_does_not_follow_post_redirects() -> None:
    sent: list[str] = []
    transport = HttpxCompletionTransport(
        client=OutboundHttpClient(
            policy=OutboundPolicy(max_redirects=0),
            resolver=resolver("8.8.8.8"),
            transport_factory=lambda _: httpx.MockTransport(
                lambda request: (
                    sent.append(str(request.url))
                    or httpx.Response(302, headers={"location": "https://other.example/chat"})
                )
            ),
        )
    )
    with pytest.raises(LlmError):
        transport.post_json(
            "https://example.com/chat",
            headers={"Authorization": "Bearer secret"},
            payload={"prompt": "private"},
            timeout=1,
        )
    assert sent == ["https://example.com/chat"]


def test_llm_transport_blocks_unsafe_url_without_auth_leak() -> None:
    with pytest.raises(LlmError) as exc:
        HttpxCompletionTransport().post_json(
            "https://127.0.0.1/chat?token=secret",
            headers={"Authorization": "Bearer secret"},
            payload={},
            timeout=1,
        )
    text = str(exc.value)
    assert "Bearer secret" not in text
    assert "token=secret" not in text


def test_proxy_authorization_is_stripped_before_initial_request_case_insensitive() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200)

    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(handler),
    )
    client.get(
        "https://example.com/feed",
        headers={"pRoXy-AuThOrIzAtIoN": "Basic proxy-secret", "Authorization": "Bearer ok"},
        timeout=1,
    )

    assert seen == [seen[0]]
    assert "proxy-authorization" not in seen[0]
    assert seen[0]["authorization"] == "Bearer ok"
    assert "proxy-secret" not in repr(seen)


@pytest.mark.parametrize("status_code", [301, 302, 303])
def test_post_redirects_that_rewrite_method_are_rejected_without_replay(status_code: int) -> None:
    sent: list[tuple[str, bytes, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(
            (str(request.url), request.content, {k.lower(): v for k, v in request.headers.items()})
        )
        return httpx.Response(status_code, headers={"location": "https://other.example/chat"})

    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(handler),
    )
    with pytest.raises(OutboundRequestError) as exc:
        client.post_json(
            "https://example.com/chat",
            headers={"Authorization": "Bearer secret"},
            payload={"prompt": "private"},
            timeout=1,
        )

    assert exc.value.kind is OutboundErrorKind.UNSAFE_REDIRECT
    assert [url for url, _body, _headers in sent] == ["https://example.com/chat"]
    assert b"private" in sent[0][1]


@pytest.mark.parametrize("status_code", [307, 308])
def test_post_cross_origin_preserving_redirect_is_rejected_without_body_or_auth_replay(
    status_code: int,
) -> None:
    sent: list[tuple[str, bytes, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(
            (str(request.url), request.content, {k.lower(): v for k, v in request.headers.items()})
        )
        if len(sent) == 1:
            return httpx.Response(status_code, headers={"location": "https://other.example/chat"})
        return httpx.Response(200)

    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(handler),
    )
    with pytest.raises(OutboundRequestError) as exc:
        client.post_json(
            "https://example.com/chat",
            headers={"Authorization": "Bearer secret"},
            payload={"prompt": "private"},
            timeout=1,
        )

    assert exc.value.kind is OutboundErrorKind.UNSAFE_REDIRECT
    assert [url for url, _body, _headers in sent] == ["https://example.com/chat"]
    assert sent[0][2]["authorization"] == "Bearer secret"


def test_post_same_origin_307_redirect_may_replay_body_to_same_origin_only() -> None:
    sent: list[tuple[str, bytes, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(
            (str(request.url), request.content, {k.lower(): v for k, v in request.headers.items()})
        )
        if len(sent) == 1:
            return httpx.Response(307, headers={"location": "/chat2"})
        return httpx.Response(200, content=b"ok")

    client = OutboundHttpClient(
        resolver=resolver("8.8.8.8"),
        transport_factory=lambda _: httpx.MockTransport(handler),
    )
    response = client.post_json(
        "https://example.com/chat",
        headers={"Authorization": "Bearer same-origin"},
        payload={"prompt": "private"},
        timeout=1,
    )

    assert response.status_code == 200
    assert [url for url, _body, _headers in sent] == [
        "https://example.com/chat",
        "https://example.com/chat2",
    ]
    assert sent[0][1] == sent[1][1]
    assert sent[1][2]["authorization"] == "Bearer same-origin"


def test_empty_resolver_result_is_typed_dns_failure_before_request() -> None:
    sent: list[str] = []
    client = OutboundHttpClient(
        resolver=lambda host, port: [],
        transport_factory=lambda _: httpx.MockTransport(
            lambda request: sent.append(str(request.url)) or httpx.Response(200)
        ),
    )

    with pytest.raises(OutboundRequestError) as exc:
        client.get("https://example.com/feed", headers={}, timeout=1)

    assert exc.value.kind is OutboundErrorKind.DNS_FAILURE
    assert sent == []
