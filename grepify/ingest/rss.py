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
from grepify.ingest.base import Fetcher, FetchOutcome, RawItem
from grepify.ingest.feedutil import parse_feed_bytes, raw_item_from_feed_entry
from grepify.ingest.http import HttpxTransport, Transport, get_or_raise, safe_url_for_log
from grepify.ingest.ladder import alt_endpoint_urls, discover_feed_url, site_root
from grepify.models import Rung, Source, SourceKind

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
        """Fetch the primary feed only (rung 0), preserving conditional GET.

        This is the single-rung path every non-orchestrator caller keeps: the
        ladder (:meth:`acquire`) is what the orchestrator drives so it can
        record which rung served.
        """
        return self._fetch_feed(source, source.url, use_cache=True)

    def acquire(self, source: Source) -> FetchOutcome:
        """Walk the acquisition ladder (ADR 0002 §1) and report the rung.

        Order: direct (rung 0) -> alternate endpoints (rung 1) -> feed
        autodiscovery (rung 2) -> a maintainer-pinned mirror in ``active_url``
        (rung 3). A rung *advances* only when the one before it **failed**
        (raised :class:`~grepify.errors.FetchError`); a rung that returns a
        parseable feed serves it and stops, even when that feed is empty (a
        quiet feed is not a failure and must not be re-classified as degraded).
        Attempts are bounded - one GET per static rung plus one HTML + one feed
        GET for autodiscovery - so the ladder can never spin (no unbounded
        retries, PRD §9).
        """
        errors: list[str] = []
        for rung, url in self._static_candidates(source):
            try:
                items = self._fetch_feed(source, url, use_cache=rung is Rung.DIRECT)
            except FetchError as exc:
                errors.append(str(exc))
                continue
            return FetchOutcome(items, rung, None if rung is Rung.DIRECT else url)

        discovered = self._autodiscover(source, errors)
        for rung, url in self._discovered_candidates(source, discovered):
            try:
                items = self._fetch_feed(source, url, use_cache=False)
            except FetchError as exc:
                errors.append(str(exc))
                continue
            return FetchOutcome(items, rung, url)

        raise FetchError(f"{source.source_id}: all acquisition rungs failed: {'; '.join(errors)}")

    def _static_candidates(self, source: Source) -> list[tuple[Rung, str]]:
        """Rungs 0 and 1: the direct feed then same-publisher alternates."""
        candidates: list[tuple[Rung, str]] = [(Rung.DIRECT, source.url)]
        candidates += [(Rung.ALT_ENDPOINT, url) for url in alt_endpoint_urls(source.url)]
        return candidates

    def _discovered_candidates(
        self, source: Source, discovered: str | None
    ) -> list[tuple[Rung, str]]:
        """Rungs 2 and 3, in ADR order: an autodiscovered feed then, last, the
        maintainer-pinned mirror in ``active_url`` (a known-good alternate)."""
        candidates: list[tuple[Rung, str]] = []
        if discovered is not None:
            candidates.append((Rung.AUTODISCOVERY, discovered))
        if source.active_url:
            candidates.append((Rung.MIRROR, source.active_url))
        return candidates

    def _autodiscover(self, source: Source, errors: list[str]) -> str | None:
        root = site_root(source.url)
        try:
            response = get_or_raise(
                self._transport,
                root,
                headers={"user-agent": _USER_AGENT},
                timeout=self._timeout,
                source_id=source.source_id,
            )
        except FetchError as exc:
            errors.append(str(exc))
            return None
        if not (200 <= response.status_code < 300):
            safe_root = safe_url_for_log(root)
            errors.append(
                f"{source.source_id}: autodiscovery GET {safe_root} HTTP {response.status_code}"
            )
            return None
        return discover_feed_url(response.content, base_url=root)

    def _fetch_feed(self, source: Source, url: str, *, use_cache: bool) -> list[RawItem]:
        headers = {"user-agent": _USER_AGENT, "accept": _ACCEPT}
        state = self._cache.get(url) if use_cache else None
        if state is not None:
            if state.etag:
                headers["if-none-match"] = state.etag
            if state.last_modified:
                headers["if-modified-since"] = state.last_modified

        response = get_or_raise(
            self._transport,
            url,
            headers=headers,
            timeout=self._timeout,
            source_id=source.source_id,
        )

        if response.status_code == 304:
            return []
        if not (200 <= response.status_code < 300):
            raise FetchError(f"{source.source_id}: HTTP {response.status_code}")

        parsed = parse_feed_bytes(response.content, source_id=source.source_id)
        if use_cache:
            self._cache[url] = _ConditionalGetState(
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
            )

        feed_lang = parsed.feed.get("language")
        return [raw_item_from_feed_entry(entry, lang=feed_lang) for entry in parsed.entries]
