"""GRP-50: X (Twitter) fetcher behind the GRP-10 ``Fetcher`` contract.

X is a **best-effort** source class (PRD §13): the site is fully functional
without it, so this fetcher's whole job is to turn watched-handle tweets into
:class:`~grepify.ingest.base.RawItem`s *when it can* and to degrade to a logged
skip when it cannot - never to fail the run. It plugs into the same
:class:`~grepify.ingest.base.Fetcher` interface every other source kind uses;
adding it to a :class:`~grepify.ingest.registry.FetcherRegistry` is one
``register`` call (E1 brief), no orchestrator change.

This module is deliberately library-neutral: it knows nothing about twscrape.
The tweet-fetching seam is the :class:`TweetSource` protocol (mirroring
:class:`~grepify.ingest.http.Transport` for the HTTP fetchers), so tests drive
the fetcher with :class:`FakeTweetSource` and canned :class:`Tweet`s and never
touch the network. The real twscrape-backed source and its account/session
management live in :mod:`grepify.ingest.x_twscrape` (optional ``x`` extra).

Tweet text -> ``RawItem.title``
-------------------------------
The PRD §6 ``items`` schema has no tweet-text or metrics column (the same reason
:mod:`grepify.ingest.reddit` drops a post's ``score``). The tweet text is the
only field carrying keyword signal, so it becomes ``title`` (collapsed to one
line) - keyword extraction (E2) reads ``title`` + ``summary`` unchanged, and the
items browser / keyword pages show it. Engagement metrics are deliberately
dropped: there is nowhere in the contract to put them.

since_id tracking (F-ING-05)
----------------------------
:data:`SinceIdProvider` maps a ``source_id`` to the highest tweet id already
stored for it, so the source is asked only for tweets newer than the last seen
one. The default provider returns ``None`` (fetch recent and let ``item_id``
dedup keep the run idempotent - still correct, just less efficient); the
``ingest`` CLI wires a real one derived from truth (max stored ``external_id``
per X source). It is derived from truth rather than a separate committed state
file because CI containers are ephemeral (PRD §5) and truth on the ``data``
branch is the one durable store - deriving keeps since_id from ever drifting
from what was actually written.

Failure modes (the isolation contract, PRD §9/§13)
--------------------------------------------------
Every per-source failure surfaces as :class:`~grepify.errors.FetchError`, which
the orchestrator (GRP-15) catches, logs as an ``error`` ``fetch_log`` row, and
steps past - one dead X source never fails the run. This covers:

- a login **challenge**, a **rate limit**, or a **suspended/locked** account
  (the three documented twscrape failure modes, mapped in
  :mod:`grepify.ingest.x_twscrape` and classified by :func:`classify_x_failure`);
- **no accounts configured / twscrape not installed** - constructing
  :class:`XFetcher` with ``source=None`` makes every ``fetch`` a logged skip, so
  an enabled X source with no live wiring degrades exactly like a rate limit
  instead of hitting the systemic "no fetcher for kind" ``KeyError``.

An empty result (no tweets since ``since_id``) is a normal ``return []``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from grepify.errors import FetchError
from grepify.ingest.base import Fetcher, RawItem
from grepify.models import Item, Source, SourceKind

_ITEM_CAP = 50  # F-ING-06

# source_id -> highest tweet id already stored for that source (or None).
SinceIdProvider = Callable[[str], str | None]


def no_since_id(_source_id: str) -> str | None:
    """Default :data:`SinceIdProvider`: no watermark, fetch recent tweets and
    rely on ``item_id`` dedup for idempotency (see module docstring)."""
    return None


def latest_since_ids(items: Iterable[Item]) -> dict[str, str]:
    """Derive per-source since_id watermarks from stored items (F-ING-05).

    Maps each X source's ``source_id`` to the highest tweet id already stored for
    it (compared numerically - tweet ids are snowflake ints of varying length,
    so lexicographic max would be wrong). Non-X items and items whose
    ``external_id`` is not a positive integer are ignored. The ``ingest`` CLI
    turns this into a :data:`SinceIdProvider` (``mapping.get``); deriving from
    truth keeps the watermark from drifting from what was actually written and
    needs no separate committed state (see the module docstring).
    """
    watermarks: dict[str, int] = {}
    for item in items:
        if item.kind is not SourceKind.X or not item.external_id:
            continue
        try:
            tweet_id = int(item.external_id)
        except ValueError:
            continue
        current = watermarks.get(item.source_id)
        if current is None or tweet_id > current:
            watermarks[item.source_id] = tweet_id
    return {source_id: str(value) for source_id, value in watermarks.items()}


class Tweet(BaseModel):
    """One tweet, in a twscrape-neutral shape (the :class:`TweetSource` output).

    ``frozen`` + ``extra="forbid"`` catch a source adapter emitting a
    wrong-shaped record at the boundary rather than letting it into mapping.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str  # tweet id (snowflake), as a string - identity for dedup + since_id
    url: str  # canonical tweet permalink
    text: str  # full tweet text
    author: str | None = None
    created_at: str | None = None  # ISO-8601 if the source provided one
    lang: str | None = None


class TweetSource(Protocol):
    """What :class:`XFetcher` needs to fetch tweets - the injectable seam.

    Implementations return the handle's tweets newer than ``since_id`` (all
    recent tweets when ``since_id`` is ``None``), newest-first or oldest-first is
    fine (the normalizer's identity is order-independent). Any failure MUST be
    raised as :class:`~grepify.errors.FetchError` so the fetcher stays isolated
    (see module docstring); the real implementation
    (:class:`~grepify.ingest.x_twscrape.TwscrapeTweetSource`) maps every
    twscrape failure onto it.
    """

    def tweets(self, handle: str, *, since_id: str | None, limit: int) -> list[Tweet]: ...


class FakeTweetSource:
    """Fixture-based :class:`TweetSource` double (shipped, not in ``tests/``).

    Like :class:`~grepify.ingest.fake.FakeFetcher`, this is the way every X test
    drives the fetcher without a network: canned :class:`Tweet`s per handle, or
    a canned :class:`~grepify.errors.FetchError` to exercise the isolation path
    (challenge / rate limit / suspended). ``.calls`` records each
    ``(handle, since_id)`` so a test can assert since_id was threaded through.
    """

    def __init__(
        self,
        results: Mapping[str, Sequence[Tweet] | FetchError] | None = None,
        *,
        default: Sequence[Tweet] | None = None,
    ) -> None:
        self._results: dict[str, Sequence[Tweet] | FetchError] = dict(results or {})
        self._default: Sequence[Tweet] = tuple(default or ())
        self.calls: list[tuple[str, str | None]] = []

    def tweets(self, handle: str, *, since_id: str | None, limit: int) -> list[Tweet]:
        self.calls.append((handle, since_id))
        outcome = self._results.get(handle, self._default)
        if isinstance(outcome, FetchError):
            raise outcome
        return [t for t in outcome if since_id is None or int(t.id) > int(since_id)][:limit]


class XFetcher(Fetcher):
    """X fetcher (kind ``x``). See the module docstring for the whole contract."""

    def __init__(
        self,
        source: TweetSource | None,
        *,
        since_ids: SinceIdProvider = no_since_id,
        limit: int = _ITEM_CAP,
    ) -> None:
        self._source = source
        self._since_ids = since_ids
        self._limit = limit

    @property
    def kind(self) -> SourceKind:
        return SourceKind.X

    def fetch(self, source: Source) -> list[RawItem]:
        if self._source is None:
            # No accounts / twscrape not installed: a logged skip, not a crash
            # (PRD §13 - X is best-effort). Registered anyway so an enabled X
            # source never hits the systemic KeyError for an unknown kind.
            raise FetchError(
                f"{source.source_id}: x fetcher not configured "
                "(no burner accounts / twscrape extra not installed) - skipping"
            )

        handle = handle_of(source)
        since_id = self._since_ids(source.source_id)
        try:
            tweets = self._source.tweets(handle, since_id=since_id, limit=self._limit)
        except FetchError:
            raise
        except Exception as exc:  # the isolation boundary: any
            # unexpected fault from the source becomes a per-source FetchError so
            # X can never fail the run (PRD §9). The declared contract is that
            # TweetSource already raises FetchError; this is belt-and-suspenders.
            raise FetchError(f"{source.source_id}: x fetch failed: {exc}") from exc

        return [_tweet_to_raw_item(tweet) for tweet in tweets[: self._limit]]


def handle_of(source: Source) -> str:
    """Extract the bare handle from an X :class:`~grepify.models.Source`.

    ``source.url`` is the canonical ``https://x.com/<handle>`` the
    ``ConfigProvider`` resolves (PRD §7 ``SourceSpec.canonical_url``); the path
    is the handle. A URL without a path component falls back to the whole
    stripped string so a mis-shaped config still yields *something* to query
    (which then fails as a normal per-source ``FetchError`` upstream, not here).
    """
    path = urlsplit(source.url).path.strip("/")
    return path or source.url.strip()


def _tweet_to_raw_item(tweet: Tweet) -> RawItem:
    return RawItem(
        url=tweet.url,
        title=_collapse(tweet.text),
        external_id=tweet.id,
        author=tweet.author,
        published_at=tweet.created_at,
        lang=tweet.lang,
    )


def _collapse(text: str) -> str:
    """Collapse a tweet's (possibly multi-line) text to a single whitespace-run
    line for the ``title`` column - the same normalization
    ``feedutil.clean_title`` gives feed titles, minus tag stripping (tweets are
    plain text, not markup)."""
    return " ".join(text.split())


def classify_x_failure(exc: BaseException) -> str:
    """Classify a raw twscrape/transport exception into one of the documented X
    failure modes for the :class:`~grepify.errors.FetchError` message.

    Pure string classification over the exception's type name + message so it is
    testable without twscrape installed (the real mapping site is
    :class:`~grepify.ingest.x_twscrape.TwscrapeTweetSource`). Returns one of
    ``"challenge"``, ``"rate limit"``, ``"suspended"``, or ``"error"`` (an
    unrecognized fault still degrades to a logged skip - it is only the label
    that is generic).
    """
    text = f"{type(exc).__name__} {exc}".lower()
    if "challenge" in text or "captcha" in text or "denied" in text:
        return "challenge"
    if "ratelimit" in text or "rate limit" in text or "429" in text or "too many" in text:
        return "rate limit"
    if "suspend" in text or "locked" in text or "banned" in text or "unavailable" in text:
        return "suspended"
    return "error"
