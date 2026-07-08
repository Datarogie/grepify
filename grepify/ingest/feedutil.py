"""Shared feedparser helpers for the RSS/YouTube fetchers (and Reddit's
``.rss`` fallback) — GRP-11/12/13.

All three walk the same shape — an RSS/Atom feed parsed by ``feedparser`` —
even though only RSS and YouTube treat it as their primary source; factoring
identity/date/title handling here keeps the three fetchers' feed-entry mapping
identical instead of three near-copies drifting apart.

Failure modes
-------------
:func:`parse_feed_bytes` raises :class:`~grepify.errors.FetchError` only when
feedparser could not extract *any* entries from the content (fully unparseable
markup). A feed feedparser partially recovered (``bozo`` set but entries
present) is tolerated — PRD §8 F-ING-01's malformed-feed tolerance: broken
markup elsewhere in the document doesn't cost the entries feedparser could
still read. :func:`entry_published_at` and :func:`clean_title` are pure and
never raise: a date feedparser could not parse yields ``None`` (the normalizer
then falls back to ``fetched_at``), and title cleaning always returns some
string, even for empty input.
"""

from __future__ import annotations

import calendar
import html
import re
from datetime import UTC, datetime
from typing import Any

import feedparser

from grepify.clock import to_iso
from grepify.errors import FetchError
from grepify.ingest.base import RawItem

_TAG_RE = re.compile(r"<[^>]+>")


def parse_feed_bytes(content: bytes, *, source_id: str) -> Any:
    """Parse ``content`` as an RSS/Atom feed. See module docstring for the
    malformed-feed tolerance rule."""
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise FetchError(f"{source_id}: unparseable feed: {parsed.bozo_exception!r}")
    return parsed


def clean_title(raw_title: str) -> str:
    """Best-effort plain-text title: strip markup, unescape entities, collapse
    whitespace. Fetchers own display-ready title text (E1 brief) — the
    normalizer does not sanitize it further."""
    without_tags = _TAG_RE.sub(" ", raw_title)
    return " ".join(html.unescape(without_tags).split())


def entry_published_at(entry: Any) -> str | None:
    """ISO-8601 published date for a feedparser entry, or ``None`` if the
    source gave no date feedparser could parse (the normalizer then falls back
    to ``fetched_at``, PRD §6)."""
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct is None:
        return None
    return to_iso(datetime.fromtimestamp(calendar.timegm(struct), tz=UTC))


def raw_item_from_feed_entry(
    entry: Any, *, external_id: str | None = None, lang: str | None = None
) -> RawItem:
    """Build a :class:`~grepify.ingest.base.RawItem` from one feedparser entry.

    ``external_id`` overrides the entry's own ``id`` (YouTube passes its
    parsed ``yt:videoId``); ``lang`` is a feed-level fallback used only when
    the entry itself carries none.
    """
    return RawItem(
        url=entry.get("link") or "",
        title=clean_title(entry.get("title") or ""),
        external_id=external_id if external_id is not None else (entry.get("id") or None),
        summary=entry.get("summary"),
        author=entry.get("author"),
        published_at=entry_published_at(entry),
        lang=entry.get("language") or lang,
    )
