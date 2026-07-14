"""Centralized outbound HTTP policy for repository-owned network fetches."""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpcore
import httpx

from grepify.errors import FetchError

_SENSITIVE_QUERY_NAMES = frozenset(
    {"token", "key", "api_key", "apikey", "access_token", "auth", "signature", "sig"}
)
_SENSITIVE_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_ALLOWED_PORTS = frozenset({80, 443})
type SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)


class OutboundErrorKind(StrEnum):
    UNSUPPORTED_SCHEME = "unsupported scheme"
    EMBEDDED_CREDENTIALS = "embedded credentials"
    INVALID_HOST = "invalid host"
    UNSAFE_DESTINATION = "unsafe destination"
    DNS_FAILURE = "DNS resolution failure"
    UNSAFE_REDIRECT = "unsafe redirect"
    REDIRECT_LOOP = "redirect loop"
    REDIRECT_LIMIT = "redirect limit exceeded"
    TLS_DOWNGRADE = "TLS downgrade"
    NETWORK_TIMEOUT = "network timeout"
    RESPONSE_FAILURE = "response failure"


class OutboundRequestError(FetchError):
    def __init__(self, kind: OutboundErrorKind, message: str) -> None:
        self.kind = kind
        super().__init__(f"{kind.value}: {message}")


@dataclass(frozen=True)
class HttpCompatibility:
    allowed_hosts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OutboundPolicy:
    http: HttpCompatibility = HttpCompatibility()
    max_redirects: int = 5


@dataclass(frozen=True)
class ValidatedUrl:
    raw: str
    scheme: str
    host: str
    port: int
    origin: tuple[str, str, int]


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    content: bytes
    headers: dict[str, str]


class Resolver(Protocol):
    def __call__(
        self, host: str, port: int
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]: ...


class Transport(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse: ...


class _BoundResolver:
    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._cache: dict[tuple[str, int], list[ipaddress.IPv4Address | ipaddress.IPv6Address]] = {}

    def validate(self, host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        addresses = self._resolver(host, port)
        if not addresses:
            raise OutboundRequestError(OutboundErrorKind.DNS_FAILURE, host)
        for address in addresses:
            _reject_unsafe_address(address)
        self._cache[(host, port)] = addresses
        return addresses

    def bound(self, host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            return self._cache[(host, port)]
        except KeyError as exc:
            raise OutboundRequestError(OutboundErrorKind.UNSAFE_DESTINATION, host) from exc


class _PolicyNetworkBackend(httpcore.NetworkBackend):
    def __init__(self, resolver: _BoundResolver) -> None:
        self._resolver = resolver
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.NetworkStream:
        addresses = self._resolver.bound(host, port)
        if not addresses:
            raise OutboundRequestError(OutboundErrorKind.DNS_FAILURE, host)
        errors: list[Exception] = []
        for address in addresses:
            try:
                return self._backend.connect_tcp(
                    str(address), port, timeout, local_address, socket_options
                )
            except Exception as exc:
                errors.append(exc)
        raise OutboundRequestError(OutboundErrorKind.RESPONSE_FAILURE, host) from errors[-1]


class _PolicyTransport(httpx.HTTPTransport):
    def __init__(self, *, resolver: _BoundResolver, ssl_context: ssl.SSLContext | None) -> None:
        super().__init__(
            verify=ssl_context or ssl.create_default_context(), trust_env=False, retries=0
        )
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context or ssl.create_default_context(),
            retries=0,
            network_backend=_PolicyNetworkBackend(resolver),
        )


class OutboundHttpClient:
    def __init__(
        self,
        *,
        policy: OutboundPolicy | None = None,
        resolver: Resolver | None = None,
        transport_factory: Callable[[Resolver], httpx.BaseTransport] | None = None,
    ) -> None:
        self._policy = policy or OutboundPolicy()
        self._resolver = resolver or _resolve_public_addresses
        self._transport_factory = transport_factory

    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        return self._request("GET", url, headers=headers, timeout=timeout)

    def post_json(
        self, url: str, *, headers: Mapping[str, str], payload: Mapping[str, Any], timeout: float
    ) -> HttpResponse:
        return self._request("POST", url, headers=headers, json=dict(payload), timeout=timeout)

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        json: Mapping[str, Any] | None = None,
    ) -> HttpResponse:
        current = _validate_url(url, self._policy)
        seen = {current.raw}
        safe_headers = _safe_initial_headers(headers)
        ssl_context = ssl.create_default_context()
        bound_resolver = _BoundResolver(self._resolver)
        transport = (
            self._transport_factory(bound_resolver.validate)
            if self._transport_factory is not None
            else _PolicyTransport(resolver=bound_resolver, ssl_context=ssl_context)
        )
        try:
            with httpx.Client(
                transport=transport, follow_redirects=False, trust_env=False
            ) as client:
                for hop in range(self._policy.max_redirects + 1):
                    bound_resolver.validate(current.host, current.port)
                    if method == "POST":
                        response = client.post(
                            current.raw, headers=dict(safe_headers), json=json, timeout=timeout
                        )
                    else:
                        response = client.get(
                            current.raw, headers=dict(safe_headers), timeout=timeout
                        )
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        return _to_response(response)
                    location = response.headers.get("location")
                    if not location:
                        raise OutboundRequestError(
                            OutboundErrorKind.UNSAFE_REDIRECT, "redirect response missing Location"
                        )
                    if hop >= self._policy.max_redirects:
                        raise OutboundRequestError(
                            OutboundErrorKind.REDIRECT_LIMIT, _redact_url(current.raw)
                        )
                    next_url = str(httpx.URL(current.raw).join(location))
                    nxt = _validate_url(next_url, self._policy)
                    _validate_redirect_method(method, response.status_code, current, nxt)
                    if current.scheme == "https" and nxt.scheme == "http":
                        raise OutboundRequestError(
                            OutboundErrorKind.TLS_DOWNGRADE, _redact_url(nxt.raw)
                        )
                    if nxt.raw in seen:
                        raise OutboundRequestError(
                            OutboundErrorKind.REDIRECT_LOOP, _redact_url(nxt.raw)
                        )
                    seen.add(nxt.raw)
                    if nxt.origin != current.origin:
                        safe_headers = _strip_cross_origin_headers(safe_headers)
                    current = nxt
        except httpx.TimeoutException as exc:
            raise OutboundRequestError(
                OutboundErrorKind.NETWORK_TIMEOUT, _redact_url(current.raw)
            ) from exc
        except httpx.HTTPError as exc:
            raise OutboundRequestError(
                OutboundErrorKind.RESPONSE_FAILURE, _redact_url(current.raw)
            ) from exc
        raise OutboundRequestError(OutboundErrorKind.REDIRECT_LIMIT, _redact_url(current.raw))


class HttpxTransport:
    def __init__(self, *, client: OutboundHttpClient | None = None) -> None:
        self._client = client or OutboundHttpClient()

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
        return self._client.get(url, headers=headers, timeout=timeout)


def _validate_url(url: str, policy: OutboundPolicy) -> ValidatedUrl:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise OutboundRequestError(
            OutboundErrorKind.INVALID_HOST, "URL contains control characters"
        )
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"}:
        raise OutboundRequestError(OutboundErrorKind.UNSUPPORTED_SCHEME, _redact_url(url))
    if parts.scheme == "http":
        host_for_policy = _normalize_host(parts.hostname or "")
        if host_for_policy not in policy.http.allowed_hosts:
            raise OutboundRequestError(
                OutboundErrorKind.UNSUPPORTED_SCHEME, "HTTP is not enabled for this host"
            )
    if parts.username or parts.password or "@" in parts.netloc.rsplit("]", 1)[-1]:
        raise OutboundRequestError(OutboundErrorKind.EMBEDDED_CREDENTIALS, _redact_url(url))
    if parts.fragment:
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
        parts = urlsplit(url)
    host = _normalize_host(parts.hostname or "")
    if not host:
        raise OutboundRequestError(OutboundErrorKind.INVALID_HOST, "missing host")
    try:
        port = parts.port or _DEFAULT_PORTS[parts.scheme]
    except ValueError as exc:
        raise OutboundRequestError(OutboundErrorKind.INVALID_HOST, "malformed port") from exc
    if port not in _ALLOWED_PORTS:
        raise OutboundRequestError(OutboundErrorKind.INVALID_HOST, "unsupported port")
    _validate_literal_or_dns_name(host)
    return ValidatedUrl(url, parts.scheme, host, port, (parts.scheme, host, port))


def _normalize_host(host: str) -> str:
    cleaned = host.rstrip(".").lower()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned) or "%" in cleaned:
        raise OutboundRequestError(OutboundErrorKind.INVALID_HOST, "invalid host")
    try:
        return cleaned.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise OutboundRequestError(OutboundErrorKind.INVALID_HOST, "invalid IDNA host") from exc


def _validate_literal_or_dns_name(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        if _looks_like_ip_bypass(host):
            raise OutboundRequestError(
                OutboundErrorKind.INVALID_HOST, "ambiguous IP-like host"
            ) from exc
        return
    _reject_unsafe_address(address)


def _resolve_public_addresses(
    host: str, port: int
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    literal = _literal_address(host)
    if literal is not None:
        _reject_unsafe_address(literal)
        return [literal]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OutboundRequestError(OutboundErrorKind.DNS_FAILURE, host) from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address not in seen:
            addresses.append(address)
            seen.add(address)
    if not addresses:
        raise OutboundRequestError(OutboundErrorKind.DNS_FAILURE, host)
    for address in addresses:
        _reject_unsafe_address(address)
    return addresses


def _literal_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _reject_unsafe_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        _reject_unsafe_address(address.ipv4_mapped)
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
    ):
        raise OutboundRequestError(
            OutboundErrorKind.UNSAFE_DESTINATION, "destination is not public"
        )


def _looks_like_ip_bypass(host: str) -> bool:
    lowered = host.lower()
    return lowered.startswith(("0x", "0")) or lowered.replace(".", "").isdigit() or ":" in lowered


def _validate_redirect_method(
    method: str, status_code: int, current: ValidatedUrl, nxt: ValidatedUrl
) -> None:
    if method == "GET":
        return
    if status_code in {301, 302, 303}:
        raise OutboundRequestError(
            OutboundErrorKind.UNSAFE_REDIRECT, "POST redirect would replay request body"
        )
    if nxt.origin != current.origin:
        raise OutboundRequestError(
            OutboundErrorKind.UNSAFE_REDIRECT, "POST redirect changed origin"
        )


def _safe_initial_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in {"host", "proxy-authorization"}}


def _strip_cross_origin_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADERS}


def _to_response(response: httpx.Response) -> HttpResponse:
    return HttpResponse(
        status_code=response.status_code,
        content=response.content,
        headers={key.lower(): value for key, value in response.headers.items()},
    )


def safe_url_for_log(url: str) -> str:
    return _redact_url(url)


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<malformed-url>"
    query = urlencode(
        [
            (k, "REDACTED" if k.lower() in _SENSITIVE_QUERY_NAMES else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    host = parts.hostname or "<missing-host>"
    netloc = host
    try:
        if parts.port is not None:
            netloc = f"{host}:{parts.port}"
    except ValueError:
        pass
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def get_or_raise(
    transport: Transport, url: str, *, headers: dict[str, str], timeout: float, source_id: str
) -> HttpResponse:
    try:
        return transport.get(url, headers=headers, timeout=timeout)
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"{source_id}: fetch failed") from exc
