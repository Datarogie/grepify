"""Browser-facing URL policy for generated/public artifacts.

This is intentionally separate from ingest HTTP validation: rendering a link does
not perform DNS, private-IP, redirect, or port checks. The browser policy only
answers whether a stored/configured/feed URL may be published as a navigable
link, resolving relatives against the feed/source URL when available.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import SplitResult, unquote, urljoin, urlsplit, urlunsplit

from grepify.url_authority import format_url_authority

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_C0_SPACE = "".join(chr(i) for i in range(0x21))


@dataclass(frozen=True)
class PublishedUrl:
    """A URL that is safe to place in browser-visible `href` or JSON URL fields."""

    href: str


def safe_published_url(raw: str | None, *, base_url: str | None = None) -> PublishedUrl | None:
    """Return a normalized HTTP(S) browser URL, or ``None`` when unsafe/invalid.

    Leading/trailing C0 controls and whitespace are trimmed before parsing.
    Relative and protocol-relative references are resolved against an HTTP(S)
    ``base_url``. Unsupported schemes, malformed authorities/ports, embedded
    controls/whitespace, and percent-encoded scheme smuggling are treated as no
    link rather than rendered into an ``href``.
    """
    if raw is None:
        return None
    candidate = _resolve_candidate(raw, base_url=base_url)
    if candidate is None:
        return None
    parts = _split_http_url(candidate)
    return _published_from_parts(parts) if parts is not None else None


def _resolve_candidate(raw: str, *, base_url: str | None) -> str | None:
    candidate = _trim_url(raw)
    if not candidate or _has_forbidden_chars(candidate) or _encoded_unsupported_scheme(candidate):
        return None
    base = _safe_base(base_url)
    try:
        has_scheme = bool(urlsplit(candidate).scheme)
    except ValueError:
        return None
    if has_scheme:
        return candidate
    return urljoin(base, candidate) if base is not None else None


def _split_http_url(candidate: str) -> SplitResult | None:
    try:
        parts = urlsplit(candidate)
        if parts.scheme.lower() not in _ALLOWED_SCHEMES or not parts.hostname:
            return None
        _port = parts.port  # Force port parsing; malformed ports raise ValueError lazily.
    except ValueError:
        return None
    return parts


def _published_from_parts(parts: SplitResult) -> PublishedUrl | None:
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if not host or _has_forbidden_chars(host):
        return None
    netloc = format_url_authority(scheme=scheme, host=host, port=parts.port)
    path = parts.path or "/"
    return PublishedUrl(urlunsplit((scheme, netloc, path, parts.query, parts.fragment)))


def _trim_url(value: str) -> str:
    return value.strip(_C0_SPACE)


def _has_forbidden_chars(value: str) -> bool:
    return any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value)


def _encoded_unsupported_scheme(value: str) -> bool:
    head = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if ":" not in head or "%" not in head:
        return False
    decoded = unquote(head).strip().lower()
    scheme = decoded.split(":", 1)[0]
    return bool(scheme and scheme not in _ALLOWED_SCHEMES)


def _safe_base(base_url: str | None) -> str | None:
    if base_url is None:
        return None
    safe = safe_published_url(base_url)
    return safe.href if safe is not None else None
