"""GRP-13: Reddit fetcher - ``/r/<sub>/new.json`` with UA + backoff (PRD §8
F-ING-04).

Fetches ``source.url`` - already the canonical
``https://www.reddit.com/r/<sub>/new.json`` URL (PRD §7 / ``ConfigProvider``
resolves it) - requesting ``limit=50`` directly (F-ING-06's per-run cap) so a
pathological subreddit never sends more than the fetcher will keep. A blank
User-Agent gets Reddit's default client rate-limited/blocked immediately, so a
descriptive one is always sent.

Backoff and the ``.rss`` fallback (F-ING-04)
---------------------------------------------
A 429/5xx response (or a transport-level failure) is retried with bounded
exponential backoff (``_MAX_ATTEMPTS`` attempts total). A 429 carrying a
``Retry-After`` that parses as 0-60 seconds sleeps that long instead; anything
else (absent, malformed, out of bounds) keeps the exponential delay, so a
hostile header can never stall a run. A non-retryable client
error (e.g. 403 - the documented "Reddit JSON blocked from CI IPs" failure
mode, PRD §13 risk table) short-circuits immediately rather than wasting
attempts. Once the JSON endpoint is exhausted either way, this falls back to
Reddit's documented ``.rss`` endpoint for the same listing (F-ING-04) - parsed
with the same ``feedparser`` machinery :mod:`grepify.ingest.rss` uses. The
fallback itself is a single attempt: F-ING-04's "reduced cadence" for the
fallback path is a *scheduling* decision (how often this source is fetched at
all), which belongs to the ingest orchestrator (GRP-15, not yet built) - a
single ``fetch`` call has no notion of cadence.

Field mapping
-------------
``permalink`` becomes ``RawItem.url`` (the stable discussion page, not the
outbound link - PRD §8 F-ING-04 explicitly calls out storing the permalink).
``selftext`` is passed through in full as ``summary``; the 2k excerpt cap is
the normalizer's job (GRP-14), not this fetcher's. Reddit's ``score`` has no
home in :class:`~grepify.ingest.base.RawItem` or the PRD §6 ``items`` schema -
neither carries a numeric score column - so it is read from the API response
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
from grepify.ingest.base import AcquisitionError, Fetcher, FetchOutcome, RawItem
from grepify.ingest.feedutil import clean_title, parse_feed_bytes, raw_item_from_feed_entry
from grepify.ingest.http import HttpResponse, HttpxTransport, Transport, get_or_raise
from grepify.ingest.trace import coarse_error, status_reason, trace_json, trace_row
from grepify.models import Rung, Source, SourceKind

_TIMEOUT_SECONDS = 10.0
_USER_AGENT = "grepify-ingest/0.1 (personal feed aggregator; +https://github.com/Datarogie/grepify)"
_ITEM_CAP = 50  # F-ING-06
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_MAX_RESPONSE_BYTES = 2_000_000
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
        return self.acquire(source).items

    def acquire(self, source: Source) -> FetchOutcome:
        """Try ``new.json`` (rung 0), falling back to ``.rss`` (rung 1, F-ING-04).

        The JSON endpoint is the primary; when it is blocked or exhausted - a
        403 from a CI IP is the documented Reddit failure (PRD §13) - the
        documented ``.rss`` listing is tried once before the source reads as an
        error. A ``.rss`` recovery is a fallback rung, so it surfaces as
        :attr:`~grepify.models.Rung.ALT_ENDPOINT` (degraded), letting the ~26
        best-effort Reddit sources still serve when their JSON is per-request
        blocked instead of all reading red.
        """
        headers = {"user-agent": _USER_AGENT}
        url = _with_limit(source.url, _ITEM_CAP)

        trace: list[dict[str, object]] = []
        response = self._get_with_backoff(
            url, headers=headers, source_id=source.source_id, trace=trace
        )
        if response is not None:
            try:
                items = self._parse_json(response.content, source_id=source.source_id)
            except FetchError as exc:
                trace.append(_trace_row("json", url, "error", reason=coarse_error(str(exc))))
                raise AcquisitionError(str(exc), acquisition_trace=trace_json(trace)) from exc
            capped = items[:_ITEM_CAP]
            trace.append(_trace_row("json", url, "served", items=len(capped)))
            return FetchOutcome(capped, Rung.DIRECT, acquisition_trace=trace_json(trace))
        fallback_url = _rss_fallback_url(source.url)
        try:
            items = self._fetch_rss_fallback(source, fallback_url, headers=headers)
        except FetchError as exc:
            trace.append(_trace_row("rss", fallback_url, "error", reason=coarse_error(str(exc))))
            raise AcquisitionError(str(exc), acquisition_trace=trace_json(trace)) from exc
        capped = items[:_ITEM_CAP]
        trace.append(_trace_row("rss", fallback_url, "served", items=len(capped)))
        return FetchOutcome(capped, Rung.ALT_ENDPOINT, fallback_url, trace_json(trace))

    def _get_with_backoff(
        self, url: str, *, headers: dict[str, str], source_id: str, trace: list[dict[str, object]]
    ) -> HttpResponse | None:
        """GET with bounded exponential backoff on transient failures.

        Returns the first 2xx response, or ``None`` once attempts are
        exhausted (caller falls back to ``.rss`` - F-ING-04). A non-retryable
        HTTP error (e.g. 403/404) also returns ``None`` immediately - retrying
        a hard client error would only waste attempts.
        """
        for attempt in range(self._max_attempts):
            retry_after: float | None = None
            try:
                response = get_or_raise(
                    self._transport,
                    url,
                    headers=headers,
                    timeout=self._timeout,
                    source_id=source_id,
                    max_bytes=_MAX_RESPONSE_BYTES,
                )
            except FetchError as exc:
                trace.append(_trace_row("json", url, "error", reason=coarse_error(str(exc))))
            else:
                if 200 <= response.status_code < 300:
                    return response
                status = response.status_code
                if status not in _RETRYABLE_STATUSES:
                    row = _trace_row(
                        "json", url, "error", status=status, reason=status_reason(status)
                    )
                    trace.append(row)
                    return None
                header = response.headers.get("retry-after")
                retry_after = _parse_retry_after(header)
                trace.append(
                    _trace_row(
                        "json",
                        url,
                        "retry",
                        status=status,
                        reason=status_reason(status),
                        retry_after=_retry_after_evidence(header, retry_after),
                    )
                )
            if attempt < self._max_attempts - 1:
                delay = _BACKOFF_BASE_SECONDS * (2**attempt) if retry_after is None else retry_after
                self._sleep(delay)
        return None

    def _fetch_rss_fallback(
        self, source: Source, fallback_url: str, *, headers: dict[str, str]
    ) -> list[RawItem]:
        response = get_or_raise(
            self._transport,
            fallback_url,
            headers=headers,
            timeout=self._timeout,
            source_id=source.source_id,
            max_bytes=_MAX_RESPONSE_BYTES,
        )
        if not (200 <= response.status_code < 300):
            raise FetchError(
                f"{source.source_id}: reddit json blocked and .rss fallback returned "
                f"HTTP {response.status_code}"
            )
        parsed = parse_feed_bytes(response.content, source_id=source.source_id)
        return [raw_item_from_feed_entry(entry) for entry in parsed.entries]

    def _parse_json(self, content: bytes, *, source_id: str) -> list[RawItem]:
        # One try validates the whole shape (listing and every child): a
        # malformed child is the same "body isn't the shape expected" failure
        # this fetcher turns into FetchError.
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


def _parse_retry_after(header: str | None) -> float | None:
    """Accept only a plain 0-60 second count; HTTP-dates and junk read as absent."""
    if header is None:
        return None
    try:
        seconds = float(header)
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 60 else None


def _retry_after_evidence(header: str | None, parsed: float | None) -> str:
    if header is None:
        return "absent"
    return header.strip() if parsed is not None else "unusable"


def _trace_row(method: str, url: str, outcome: str, **fields: object) -> dict[str, object]:
    return trace_row("reddit", method, url, outcome, **fields)
