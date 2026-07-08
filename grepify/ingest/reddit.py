"""GRP-13: Reddit fetcher — ``/r/<sub>/new.json`` with UA + backoff (PRD §8
F-ING-04).

Fetches ``source.url`` — already the canonical
``https://www.reddit.com/r/<sub>/new.json`` URL (PRD §7 / ``ConfigProvider``
resolves it) — requesting ``limit=50`` directly (F-ING-06's per-run cap) so a
pathological subreddit never sends more than the fetcher will keep. A blank
User-Agent gets Reddit's default client rate-limited/blocked immediately, so a
descriptive one is always sent.

Backoff and the ``.rss`` fallback (F-ING-04)
---------------------------------------------
A 429/5xx response (or a transport-level failure) is retried with bounded
exponential backoff (``_MAX_ATTEMPTS`` attempts total). A non-retryable client
error (e.g. 403 — the documented "Reddit JSON blocked from CI IPs" failure
mode, PRD §13 risk table) short-circuits immediately rather than wasting
attempts. Once the JSON endpoint is exhausted either way, this falls back to
Reddit's documented ``.rss`` endpoint for the same listing (F-ING-04) — parsed
with the same ``feedparser`` machinery :mod:`grepify.ingest.rss` uses. The
fallback itself is a single attempt: F-ING-04's "reduced cadence" for the
fallback path is a *scheduling* decision (how often this source is fetched at
all), which belongs to the ingest orchestrator (GRP-15, not yet built) — a
single ``fetch`` call has no notion of cadence.

Field mapping
-------------
``permalink`` becomes ``RawItem.url`` (the stable discussion page, not the
outbound link — PRD §8 F-ING-04 explicitly calls out storing the permalink).
``selftext`` is passed through in full as ``summary``; the 2k excerpt cap is
the normalizer's job (GRP-14), not this fetcher's. Reddit's ``score`` has no
home in :class:`~grepify.ingest.base.RawItem` or the PRD §6 ``items`` schema —
neither carries a numeric score column — so it is read from the API response
and deliberately dropped; there is nowhere in the current contract to put it.

Failure modes
-------------
Every per-source failure becomes :class:`~grepify.errors.FetchError`
(non-fatal, PRD §9): the JSON endpoint blocked/erroring *and* the ``.rss``
fallback also failing, or either endpoint returning a body that isn't the
shape expected (malformed JSON / unparseable feed). An empty listing (no new
posts) returns ``[]``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from grepify.clock import to_iso
from grepify.errors import FetchError
from grepify.ingest.base import Fetcher, RawItem
from grepify.ingest.feedutil import clean_title, parse_feed_bytes, raw_item_from_feed_entry
from grepify.ingest.http import HttpResponse, HttpxTransport, Transport, get_or_raise
from grepify.models import Source, SourceKind

_TIMEOUT_SECONDS = 10.0
_USER_AGENT = "grepify-ingest/0.1 (personal feed aggregator; +https://github.com/Datarogie/grepify)"
_ITEM_CAP = 50  # F-ING-06
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _with_limit(url: str, limit: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["limit"] = str(limit)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _rss_fallback_url(url: str) -> str:
    """Reddit's documented ``.rss`` listing endpoint for the same subreddit
    (F-ING-04). ``source.url`` is always the ``.json`` form the
    ``ConfigProvider`` resolves (PRD §7 ``SourceSpec.canonical_url``)."""
    if url.endswith(".json"):
        return url[: -len(".json")] + ".rss"
    return url.rstrip("/") + ".rss"  # defensive: unexpected url shape


class RedditFetcher(Fetcher):
    """Reddit ``new.json`` fetcher, with ``.rss`` fallback (PRD §8 F-ING-04)."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        timeout: float = _TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> None:
        self._transport = transport or HttpxTransport()
        self._timeout = timeout
        self._sleep = sleep
        self._max_attempts = max_attempts

    @property
    def kind(self) -> SourceKind:
        return SourceKind.REDDIT

    def fetch(self, source: Source) -> list[RawItem]:
        headers = {"user-agent": _USER_AGENT}
        url = _with_limit(source.url, _ITEM_CAP)

        response = self._get_with_backoff(url, headers=headers, source_id=source.source_id)
        if response is not None:
            items = self._parse_json(response.content, source_id=source.source_id)
        else:
            items = self._fetch_rss_fallback(source, headers=headers)
        return items[:_ITEM_CAP]

    def _get_with_backoff(
        self, url: str, *, headers: dict[str, str], source_id: str
    ) -> HttpResponse | None:
        """GET with bounded exponential backoff on transient failures.

        Returns the first 2xx response, or ``None`` once attempts are
        exhausted (caller falls back to ``.rss`` — F-ING-04). A non-retryable
        HTTP error (e.g. 403/404) also returns ``None`` immediately — retrying
        a hard client error would only waste attempts.
        """
        for attempt in range(self._max_attempts):
            try:
                response = get_or_raise(
                    self._transport,
                    url,
                    headers=headers,
                    timeout=self._timeout,
                    source_id=source_id,
                )
            except FetchError:
                pass
            else:
                if 200 <= response.status_code < 300:
                    return response
                if response.status_code not in _RETRYABLE_STATUSES:
                    return None
            if attempt < self._max_attempts - 1:
                self._sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
        return None

    def _fetch_rss_fallback(self, source: Source, *, headers: dict[str, str]) -> list[RawItem]:
        fallback_url = _rss_fallback_url(source.url)
        response = get_or_raise(
            self._transport,
            fallback_url,
            headers=headers,
            timeout=self._timeout,
            source_id=source.source_id,
        )
        if not (200 <= response.status_code < 300):
            raise FetchError(
                f"{source.source_id}: reddit json blocked and .rss fallback returned "
                f"HTTP {response.status_code}"
            )
        parsed = parse_feed_bytes(response.content, source_id=source.source_id)
        return [raw_item_from_feed_entry(entry) for entry in parsed.entries]

    def _parse_json(self, content: bytes, *, source_id: str) -> list[RawItem]:
        # The whole shape - top-level listing AND every child - is validated in
        # one try: a malformed child (missing "data", a non-numeric
        # created_utc, ...) is exactly the "body isn't the shape expected"
        # failure mode this fetcher promises to turn into FetchError, same as
        # a top-level JSON parse failure.
        try:
            payload = json.loads(content)
            children = payload["data"]["children"]
            return [self._child_to_raw_item(child["data"]) for child in children]
        except (
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
            OSError,
        ) as exc:
            raise FetchError(f"{source_id}: malformed reddit json: {exc}") from exc

    def _child_to_raw_item(self, data: dict[str, Any]) -> RawItem:
        permalink = data.get("permalink") or ""
        url = f"https://www.reddit.com{permalink}" if permalink else (data.get("url") or "")
        created_utc = data.get("created_utc")
        published_at = (
            to_iso(datetime.fromtimestamp(created_utc, tz=UTC)) if created_utc is not None else None
        )
        return RawItem(
            url=url,
            title=clean_title(data.get("title") or ""),
            external_id=data.get("id") or None,
            summary=data.get("selftext") or None,
            author=data.get("author"),
            published_at=published_at,
        )
