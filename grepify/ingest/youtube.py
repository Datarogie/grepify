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

Transient 5xx retry (T5 audit, GRP-30)
---------------------------------------
The fetch-log audit found channel fetches intermittently returning ``HTTP
5xx`` that recovered on a later run with no code change - a transient
YouTube-side hiccup, not a dead channel. :meth:`fetch` now retries a ``5xx``
*response* with bounded exponential backoff (``_MAX_ATTEMPTS`` attempts total,
same shape as :class:`~grepify.ingest.reddit.RedditFetcher`'s backoff), before
raising. A ``4xx`` (e.g. a deleted/renamed channel) is **not** retried -
retrying a hard client error would only waste attempts on something backoff
cannot fix, same reasoning as the reddit fetcher's non-retryable-status
short-circuit. A transport-level failure (DNS, TLS, connection refused,
timeout - i.e. :func:`~grepify.ingest.http.get_or_raise` raising
:class:`~grepify.errors.FetchError` before any response exists) is also
**not** retried here, unlike the reddit fetcher: this issue's audit evidence
was channel fetches getting an HTTP 5xx *response* that later succeeded, not
a transport failure, so retrying only what the evidence showed keeps this
fetcher's scope narrow and its retry semantics easy to reason about; widening
it to transport failures too is future work if that failure mode shows up
here (it has not, as of this audit).

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
outside 2xx (after retries are exhausted for a ``5xx``), or a feed feedparser
could not extract any entries from all become :class:`~grepify.errors.FetchError`
(non-fatal, PRD §9). An empty channel (no videos) returns ``[]``. Transcript
fetching never raises here (the store degrades every transcript failure to
``None``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from grepify.errors import FetchError
from grepify.ingest.base import Fetcher, RawItem
from grepify.ingest.feedutil import parse_feed_bytes, raw_item_from_feed_entry
from grepify.ingest.http import HttpResponse, HttpxTransport, Transport, get_or_raise
from grepify.ingest.transcript import TranscriptStore
from grepify.models import Source, SourceKind

_TIMEOUT_SECONDS = 10.0
_USER_AGENT = "grepify-ingest/0.1 (+https://github.com/Datarogie/grepify)"
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})


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
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        self._transport = transport or HttpxTransport()
        self._timeout = timeout
        self._transcript_store = transcript_store
        self._sleep = sleep
        self._max_attempts = max_attempts

    @property
    def kind(self) -> SourceKind:
        return SourceKind.YOUTUBE

    def fetch(self, source: Source) -> list[RawItem]:
        response = self._get_with_backoff(source)
        parsed = parse_feed_bytes(response.content, source_id=source.source_id)
        return [self._entry_to_raw_item(entry) for entry in parsed.entries]

    def _get_with_backoff(self, source: Source) -> HttpResponse:
        """GET ``source.url`` with bounded exponential backoff on a transient
        ``5xx``. A non-retryable status (2xx already returns; any other 4xx)
        raises immediately - see module docstring."""
        headers = {"user-agent": _USER_AGENT}
        response: HttpResponse | None = None
        for attempt in range(self._max_attempts):
            response = get_or_raise(
                self._transport,
                source.url,
                headers=headers,
                timeout=self._timeout,
                source_id=source.source_id,
            )
            if 200 <= response.status_code < 300:
                return response
            if response.status_code not in _RETRYABLE_STATUSES:
                raise FetchError(f"{source.source_id}: HTTP {response.status_code}")
            if attempt < self._max_attempts - 1:
                self._sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
        # Reason for the ignore below: type-narrowing invariant, not a runtime input check -
        # the loop above always runs >= 1 time (_max_attempts >= 1 is enforced
        # in __init__), so `response` is always assigned by the time we get here.
        assert response is not None  # noqa: S101
        raise FetchError(
            f"{source.source_id}: HTTP {response.status_code} after {self._max_attempts} attempts"
        )

    def _entry_to_raw_item(self, entry: Any) -> RawItem:
        item = raw_item_from_feed_entry(entry, external_id=_video_id(entry))
        video_id = entry.get("yt_videoid")
        if self._transcript_store is None or not video_id:
            return item
        transcript_ref = self._transcript_store.ensure(video_id)
        if transcript_ref is None:
            return item
        return item.model_copy(update={"transcript_ref": transcript_ref})
