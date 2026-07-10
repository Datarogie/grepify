"""GRP-12: YouTube channel-RSS fetcher - no API key (PRD §8 F-ING-02).

Fetches ``source.url`` - already the canonical
``https://www.youtube.com/feeds/videos.xml?channel_id=...`` URL (PRD §7 /
``ConfigProvider`` resolves it) - and parses YouTube's channel Atom feed the
same way :mod:`grepify.ingest.rss` parses RSS, using the feed's
``<yt:videoId>`` element (exposed by ``feedparser`` as ``entry.yt_videoid``) as
``external_id`` per F-ING-02, falling back to the entry's own ``id`` (YouTube
sets it to ``yt:video:<videoId>``) if a future feed variant ever omits
``yt:videoId``.

No conditional-GET cache here: channel feeds are small (YouTube caps them at
~15 recent videos) and this issue's scope didn't call for it - reuses the same
transport and malformed-feed tolerance as RSS (:mod:`grepify.ingest.feedutil`).

Failure modes
-------------
Same shape as :mod:`grepify.ingest.rss`: a transport failure, an HTTP status
outside 2xx, or a feed feedparser could not extract any entries from all
become :class:`~grepify.errors.FetchError` (non-fatal, PRD §9). An empty
channel (no videos) returns ``[]``.
"""

from __future__ import annotations

from typing import Any

from grepify.errors import FetchError
from grepify.ingest.base import Fetcher, RawItem
from grepify.ingest.feedutil import parse_feed_bytes, raw_item_from_feed_entry
from grepify.ingest.http import HttpxTransport, Transport, get_or_raise
from grepify.models import Source, SourceKind

_TIMEOUT_SECONDS = 10.0
_USER_AGENT = "grepify-ingest/0.1 (+https://github.com/Datarogie/grepify)"


def _video_id(entry: Any) -> str | None:
    return entry.get("yt_videoid") or (entry.get("id") or None)


class YouTubeFetcher(Fetcher):
    """YouTube channel-RSS fetcher (PRD §8 F-ING-02)."""

    def __init__(
        self, transport: Transport | None = None, *, timeout: float = _TIMEOUT_SECONDS
    ) -> None:
        self._transport = transport or HttpxTransport()
        self._timeout = timeout

    @property
    def kind(self) -> SourceKind:
        return SourceKind.YOUTUBE

    def fetch(self, source: Source) -> list[RawItem]:
        headers = {"user-agent": _USER_AGENT}
        response = get_or_raise(
            self._transport,
            source.url,
            headers=headers,
            timeout=self._timeout,
            source_id=source.source_id,
        )

        if not (200 <= response.status_code < 300):
            raise FetchError(f"{source.source_id}: HTTP {response.status_code}")

        parsed = parse_feed_bytes(response.content, source_id=source.source_id)
        return [
            raw_item_from_feed_entry(entry, external_id=_video_id(entry))
            for entry in parsed.entries
        ]
