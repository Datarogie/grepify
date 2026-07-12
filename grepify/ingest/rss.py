"""GRP-11: RSS/Atom fetcher - conditional GET, timeout, malformed tolerance.

Fetches ``source.url`` - already the canonical feed URL (PRD §7 /
``ConfigProvider`` resolves it before the fetcher ever sees it) - and parses it
with ``feedparser``, returning one :class:`~grepify.ingest.base.RawItem` per
entry. Per the fetcher contract (GRP-10), this never computes identity
(``item_id`` / ``content_hash`` / ``canonical_url``) - that's the normalizer
(GRP-14).

Fetch headers
-------------
Requests go out with a realistic browser ``user-agent`` plus a feed ``accept``
header (``application/rss+xml`` / ``application/atom+xml`` / xml). WAF-fronted
feeds (Cloudflare / Substack) answer a bot User-Agent or a request carrying no
``Accept`` header with an HTTP 403 or an HTML challenge page (which then fails
feed parsing and surfaces as ``unparseable``); the browser UA + feed Accept
header make those hosts return feed XML instead.

Conditional GET
----------------
Per-source ETag / Last-Modified pairs live in an in-memory ``dict`` supplied at
construction (or a private one created per instance). That satisfies F-ING-01's
conditional-GET requirement for the lifetime of one fetcher instance.
Persisting the cache *across separate pipeline runs* is the ingest
orchestrator's concern (GRP-15/16, not yet built - no fetch-state store exists
in the storage layer yet); the cache is a constructor parameter precisely so
the orchestrator can inject a persistent mapping later without any change here.

Failure modes
-------------
Every per-source failure becomes :class:`~grepify.errors.FetchError`
(non-fatal, PRD §9): a transport failure (timeout/DNS/connection - see
:func:`~grepify.ingest.http.get_or_raise`), an HTTP status outside 2xx/304, and
a feed feedparser could not extract *any* entries from. A feed feedparser
partially recovered from (``bozo`` set but entries present) is tolerated:
recovered entries are returned, nothing raised (F-ING-01 malformed-feed
tolerance). An unmodified feed (HTTP 304) returns ``[]`` - not an error, just
nothing new since the last conditional GET. A structurally empty feed (valid
XML, zero entries) also returns ``[]`` - an empty feed is normal, not an error.
"""

from __future__ import annotations

from dataclasses import dataclass

from grepify.errors import FetchError
from grepify.ingest.base import Fetcher, RawItem
from grepify.ingest.feedutil import parse_feed_bytes, raw_item_from_feed_entry
from grepify.ingest.http import HttpxTransport, Transport, get_or_raise
from grepify.models import Source, SourceKind

_TIMEOUT_SECONDS = 10.0
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
)


@dataclass
class _ConditionalGetState:
    etag: str | None
    last_modified: str | None


class RssFetcher(Fetcher):
    """RSS/Atom fetcher (PRD §8 F-ING-01)."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        timeout: float = _TIMEOUT_SECONDS,
        cache: dict[str, _ConditionalGetState] | None = None,
    ) -> None:
        self._transport = transport or HttpxTransport()
        self._timeout = timeout
        self._cache: dict[str, _ConditionalGetState] = cache if cache is not None else {}

    @property
    def kind(self) -> SourceKind:
        return SourceKind.RSS

    def fetch(self, source: Source) -> list[RawItem]:
        headers = {"user-agent": _USER_AGENT, "accept": _ACCEPT}
        state = self._cache.get(source.source_id)
        if state is not None:
            if state.etag:
                headers["if-none-match"] = state.etag
            if state.last_modified:
                headers["if-modified-since"] = state.last_modified

        response = get_or_raise(
            self._transport,
            source.url,
            headers=headers,
            timeout=self._timeout,
            source_id=source.source_id,
        )

        if response.status_code == 304:
            return []
        if not (200 <= response.status_code < 300):
            raise FetchError(f"{source.source_id}: HTTP {response.status_code}")

        parsed = parse_feed_bytes(response.content, source_id=source.source_id)
        self._cache[source.source_id] = _ConditionalGetState(
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

        feed_lang = parsed.feed.get("language")
        return [raw_item_from_feed_entry(entry, lang=feed_lang) for entry in parsed.entries]
