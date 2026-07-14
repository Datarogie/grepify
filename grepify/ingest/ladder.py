"""Acquisition-ladder URL derivation + feed autodiscovery (ADR 0002 §1, GRP-66).

Pure helpers behind the RSS ladder (:mod:`grepify.ingest.rss`): given a failing
primary feed URL, derive the alternate endpoints (rung 1) to try, and parse a
fetched HTML page for a declared feed link (rung 2, autodiscovery). No I/O and
no network here - the fetcher owns the GETs and hands the bytes in - so every
rung's URL math and the autodiscovery parse are unit-testable offline (the CI
egress ban means live rungs run only in the pipeline).

Autodiscovery guardrails (ADR 0002 §1 rung 2)
---------------------------------------------
Only a ``<link rel="alternate" type="application/rss+xml|atom+xml">`` is
followed, at most one, and only when it resolves to the **same registrable
host** as the source (no off-site redirect chasing). A discovered link on a
different host is ignored rather than followed, so autodiscovery cannot be
walked into an unrelated third party.

Failure modes
-------------
Pure functions over their inputs; none raise. :func:`alt_endpoint_urls` returns
``[]`` when it can derive no distinct alternate; :func:`discover_feed_url`
returns ``None`` for HTML with no same-host feed link (malformed markup is
tolerated - the parser skips what it cannot read rather than raising).
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

_FEED_TYPES = frozenset(
    {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}
)


def alt_endpoint_urls(url: str) -> list[str]:
    """Alternate same-publisher endpoints to try when ``url`` (rung 0) fails.

    Covers the WordPress-shaped bulk of the registry (ADR 0002 §1 rung 1): the
    trailing-slash variant, plus ``/feed/atom/`` and the query-based
    ``?feed=rss2`` form for a ``.../feed`` path. Deterministic and deduped; the
    original ``url`` is never returned.
    """
    parts = urlsplit(url)
    path = parts.path
    seen: set[str] = set()
    out: list[str] = []

    def add(new_path: str, query: str = parts.query) -> None:
        candidate = urlunsplit((parts.scheme, parts.netloc, new_path, query, ""))
        if candidate != url and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    add(path[:-1] if path.endswith("/") else path + "/")

    trimmed = path.rstrip("/")
    if trimmed.endswith("/feed") or trimmed == "/feed":
        stem = trimmed[: -len("/feed")]
        add(f"{stem}/feed/atom/")
        add(f"{stem}/" if stem else "/", "feed=rss2")
    return out


class _FeedLinkParser(HTMLParser):
    """Collects ``<link rel=alternate type=...feed...>`` hrefs in document order."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        attr = {name.lower(): (value or "") for name, value in attrs}
        rel = attr.get("rel", "").lower()
        type_ = attr.get("type", "").lower()
        href = attr.get("href", "")
        if "alternate" in rel.split() and type_ in _FEED_TYPES and href:
            self.hrefs.append(href)


def _same_registrable_host(a: str, b: str) -> bool:
    def host(url: str) -> str:
        return urlsplit(url).netloc.lower().removeprefix("www.")

    return host(a) == host(b)


def discover_feed_url(html: bytes, *, base_url: str) -> str | None:
    """First same-host feed link declared in ``html`` (rung 2), or ``None``.

    ``base_url`` resolves relative hrefs and enforces the same-registrable-host
    guardrail (see the module docstring). Decoding is lenient - undecodable
    bytes are replaced, never raised - since a home page is untrusted markup.
    """
    parser = _FeedLinkParser()
    parser.feed(html.decode("utf-8", errors="replace"))
    for href in parser.hrefs:
        resolved = urljoin(base_url, href)
        if _same_registrable_host(resolved, base_url):
            return resolved
    return None


def site_root(url: str) -> str:
    """The scheme://host/ home page to fetch for autodiscovery."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
