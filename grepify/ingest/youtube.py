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

Transcripts (E5, GRP-52)
------------------------
An optional :class:`~grepify.ingest.transcript.TranscriptStore` attaches a
transcript to each video: when present, the fetcher calls
:meth:`~grepify.ingest.transcript.TranscriptStore.ensure` with the bare
``yt:videoId`` and sets ``RawItem.transcript_ref`` (idempotent - already-stored
transcripts are not re-fetched, F-ING-07). Transcripts are best-effort and
absence-tolerant (F-ING-03): a video with none, or transcript fetching failing,
simply leaves ``transcript_ref=null`` and never affects the metadata fetch.
When no store is wired, behavior is exactly as before (all refs null). A video
whose feed entry lacks a ``yt:videoId`` (identity then falls back to the
``yt:video:<id>`` form) gets no transcript attempt - the store needs the bare id.

Failure modes
-------------
Same shape as :mod:`grepify.ingest.rss`: a transport failure, an HTTP status
outside 2xx, or a feed feedparser could not extract any entries from all
become :class:`~grepify.errors.FetchError` (non-fatal, PRD §9). An empty
channel (no videos) returns ``[]``. Transcript fetching never raises here (the
store degrades every transcript failure to ``None``).
"""

from __future__ import annotations

from typing import Any

from grepify.errors import FetchError
from grepify.ingest.base import Fetcher, RawItem
from grepify.ingest.feedutil import parse_feed_bytes, raw_item_from_feed_entry
from grepify.ingest.http import HttpxTransport, Transport, get_or_raise
from grepify.ingest.transcript import TranscriptStore
from grepify.models import Source, SourceKind

_TIMEOUT_SECONDS = 10.0
_USER_AGENT = "grepify-ingest/0.1 (+https://github.com/Datarogie/grepify)"


def _video_id(entry: Any) -> str | None:
    return entry.get("yt_videoid") or (entry.get("id") or None)


class YouTubeFetcher(Fetcher):
    """YouTube channel-RSS fetcher (PRD §8 F-ING-02)."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        timeout: float = _TIMEOUT_SECONDS,
        transcript_store: TranscriptStore | None = None,
    ) -> None:
        self._transport = transport or HttpxTransport()
        self._timeout = timeout
        self._transcript_store = transcript_store

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
        return [self._entry_to_raw_item(entry) for entry in parsed.entries]

    def _entry_to_raw_item(self, entry: Any) -> RawItem:
        item = raw_item_from_feed_entry(entry, external_id=_video_id(entry))
        video_id = entry.get("yt_videoid")
        if self._transcript_store is None or not video_id:
            return item
        transcript_ref = self._transcript_store.ensure(video_id)
        if transcript_ref is None:
            return item
        return item.model_copy(update={"transcript_ref": transcript_ref})
