"""Reusable Substack public archive fallback helpers.

Substack publications expose the canonical RSS URL at
``https://<publication>.substack.com/feed``. Some CI egress ranges receive a
403 from that feed even when the public archive remains readable. This module
contains the pure URL derivation and HTML extraction used by the RSS acquisition
ladder so every Substack source can share the same safe fallback behavior.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from grepify.errors import FetchError
from grepify.ingest.base import RawItem
from grepify.ingest.feedutil import clean_title


def is_substack_feed_url(url: str) -> bool:
    """Whether ``url`` is the canonical feed URL for a Substack publication."""
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()
    return (
        parts.scheme.lower() == "https"
        and host.endswith(".substack.com")
        and parts.username is None
        and parts.password is None
        and parts.query == ""
        and parts.fragment == ""
        and parts.path.rstrip("/") == "/feed"
    )


def substack_archive_url(url: str) -> str | None:
    """Return the same-publication public archive URL for a Substack feed."""
    if not is_substack_feed_url(url):
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()
    return urlunsplit(("https", host, "/archive", "", ""))


class _SubstackArchiveParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__()
        self._base_url = base_url
        self._base_host = (urlsplit(base_url).hostname or "").rstrip(".").lower()
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self.items: list[RawItem] = []
        self._seen_urls: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr = {name.lower(): (value or "") for name, value in attrs}
        href = attr.get("href", "")
        resolved = self._canonical_post_url(href)
        if resolved is None:
            return
        self._current_href = resolved
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._current_href is None:
            return
        url = self._current_href
        title = clean_title(" ".join(self._current_text))
        self._current_href = None
        self._current_text = []
        if not title or url in self._seen_urls:
            return
        self._seen_urls.add(url)
        self.items.append(RawItem(url=url, title=title, external_id=url))

    def _canonical_post_url(self, href: str) -> str | None:
        if not href:
            return None
        resolved = urljoin(self._base_url, href)
        parts = urlsplit(resolved)
        host = (parts.hostname or "").rstrip(".").lower()
        if parts.scheme.lower() != "https" or host != self._base_host:
            return None
        path = parts.path.rstrip("/")
        if not path.startswith("/p/"):
            return None
        return urlunsplit(("https", host, path, "", ""))


def parse_substack_archive_bytes(
    content: bytes, *, source_id: str, archive_url: str
) -> list[RawItem]:
    """Extract public post metadata from a same-publisher Substack archive page.

    The extractor intentionally keeps only metadata needed by grepify and
    preserves canonical ``benn.substack.com``-style post URLs. It never follows
    links discovered in the HTML; network policy remains in the caller.
    """
    parser = _SubstackArchiveParser(base_url=archive_url)
    parser.feed(content.decode("utf-8", errors="replace"))
    if not parser.items:
        raise FetchError(f"{source_id}: unparseable Substack archive: no public post links")
    return parser.items
