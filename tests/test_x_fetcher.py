"""GRP-50: X fetcher - tweet->RawItem mapping, since_id, and failure isolation.

Drives :class:`grepify.ingest.x.XFetcher` through the shipped
:class:`grepify.ingest.x.FakeTweetSource` with recorded fixtures (mirroring
xfilter's tweet payloads) and no network. The isolation tests are the heart of
the "X is best-effort" contract (PRD §13): every failure mode must surface as a
:class:`~grepify.errors.FetchError` the orchestrator can step past.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from grepify.clock import FixedClock
from grepify.config.filesystem import FilesystemConfigProvider
from grepify.errors import FetchError
from grepify.ingest import FakeFetcher, FakeTweetSource, Tweet, XFetcher
from grepify.ingest.base import RawItem
from grepify.ingest.orchestrator import IngestServices, run_ingest
from grepify.ingest.registry import FetcherRegistry
from grepify.ingest.x import classify_x_failure, handle_of, latest_since_ids, no_since_id
from grepify.models import FetchStatus, Item, SourceKind
from grepify.repository import JsonlSqliteRepository
from tests.conftest import make_source, write_config

_FIXTURE = Path(__file__).parent / "fixtures" / "x" / "tweets.json"
_CLOCK = FixedClock(datetime(2026, 7, 8, 18, 0, 0, tzinfo=UTC))


def _tweets() -> dict[str, list[Tweet]]:
    raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return {handle: [Tweet(**t) for t in tweets] for handle, tweets in raw.items()}


def _x_source(source_id: str, handle: str) -> Item:
    return make_source(source_id, kind=SourceKind.X, url=f"https://x.com/{handle}")


# --- mapping -----------------------------------------------------------------


def test_tweets_map_to_raw_items() -> None:
    source = _x_source("x-karpathy", "karpathy")
    fetcher = XFetcher(FakeTweetSource(_tweets()))

    items = fetcher.fetch(source)

    assert [i.external_id for i in items] == ["1799000000000000001", "1799000000000000002"]
    first = items[0]
    assert first.url == "https://x.com/karpathy/status/1799000000000000001"
    assert first.author == "karpathy"
    assert first.published_at == "2026-07-08T15:04:00+00:00"
    assert first.lang == "en"
    # multi-line tweet text collapses to one line for the title column.
    assert "\n" not in first.title
    assert first.title.startswith("New nanoGPT speedrun")


def test_empty_when_handle_has_no_tweets() -> None:
    fetcher = XFetcher(FakeTweetSource(_tweets()))
    assert fetcher.fetch(_x_source("x-nobody", "nobody")) == []


def test_limit_caps_returned_items() -> None:
    source = _x_source("x-karpathy", "karpathy")
    fetcher = XFetcher(FakeTweetSource(_tweets()), limit=1)
    assert len(fetcher.fetch(source)) == 1


# --- since_id ----------------------------------------------------------------


def test_since_id_is_threaded_and_filters() -> None:
    fake = FakeTweetSource(_tweets())
    # provider returns the first tweet's id -> only the second (newer) survives.
    fetcher = XFetcher(fake, since_ids=lambda _sid: "1799000000000000001")

    items = fetcher.fetch(_x_source("x-karpathy", "karpathy"))

    assert fake.calls == [("karpathy", "1799000000000000001")]
    assert [i.external_id for i in items] == ["1799000000000000002"]


def test_default_since_id_provider_is_none() -> None:
    fake = FakeTweetSource(_tweets())
    XFetcher(fake).fetch(_x_source("x-karpathy", "karpathy"))
    assert fake.calls == [("karpathy", None)]


def test_latest_since_ids_derives_numeric_max_per_source() -> None:
    items = [
        _stored_tweet("x-a", "100"),
        _stored_tweet("x-a", "99"),
        _stored_tweet("x-a", "1000"),  # numerically largest despite being shorter-sorted
        _stored_tweet("x-b", "5"),
    ]
    assert latest_since_ids(items) == {"x-a": "1000", "x-b": "5"}


def test_latest_since_ids_ignores_non_x_and_non_numeric() -> None:
    items = [
        Item(
            item_id="rss1",
            source_id="rss-src",
            kind=SourceKind.RSS,
            external_id="9999",
            canonical_url="https://e/1",
            title="t",
            published_at="2026-07-08T00:00:00+00:00",
            fetched_at="2026-07-08T00:00:00+00:00",
            content_hash="h",
        ),
        _stored_tweet("x-a", "not-a-number"),
    ]
    assert latest_since_ids(items) == {}


# --- failure isolation (the best-effort contract, PRD §13) -------------------


@pytest.mark.parametrize("mode", ["login challenge", "rate limit hit", "account suspended"])
def test_each_failure_mode_becomes_fetch_error(mode: str) -> None:
    fake = FakeTweetSource({"karpathy": FetchError(f"karpathy: x {mode}")})
    with pytest.raises(FetchError, match=mode):
        XFetcher(fake).fetch(_x_source("x-karpathy", "karpathy"))


def test_unexpected_source_exception_is_wrapped_as_fetch_error() -> None:
    class _Boom:
        def tweets(self, handle: str, *, since_id: str | None, limit: int) -> list[Tweet]:
            raise RuntimeError("twscrape internals blew up")

    with pytest.raises(FetchError, match="x-karpathy: x fetch failed"):
        XFetcher(_Boom()).fetch(_x_source("x-karpathy", "karpathy"))


def test_unconfigured_fetcher_skips_via_fetch_error() -> None:
    with pytest.raises(FetchError, match="not configured"):
        XFetcher(None).fetch(_x_source("x-karpathy", "karpathy"))


# --- pure helpers ------------------------------------------------------------


def test_handle_of_parses_canonical_url() -> None:
    assert handle_of(_x_source("x-karpathy", "karpathy")) == "karpathy"


def test_no_since_id_is_always_none() -> None:
    assert no_since_id("anything") is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("LoginChallenge: solve this captcha", "challenge"),
        ("RateLimitError: 429 too many requests", "rate limit"),
        ("AccountSuspended: this account is locked", "suspended"),
        ("SomethingWeird: unknown", "error"),
    ],
)
def test_classify_x_failure(message: str, expected: str) -> None:
    assert classify_x_failure(RuntimeError(message)) == expected


_ORCH_GROUP = """
    group: mixed
    name: Mixed
    category: ai
    sources:
      - id: good-rss
        kind: rss
        url: https://example.com/good/feed
      - id: x-karpathy
        kind: x
        handle: karpathy
"""


def test_failing_x_source_does_not_fail_the_run(tmp_path: Path) -> None:
    """The isolation contract end to end: an X source raising every failure mode
    is logged as an ``error`` fetch_log row while the rest of the run proceeds -
    X never fails the run (PRD §9/§13)."""
    cfg = write_config(tmp_path / "sources", groups={"mixed.yml": _ORCH_GROUP})
    repository = JsonlSqliteRepository(tmp_path / "data")
    registry = FetcherRegistry()
    registry.register(
        FakeFetcher(
            SourceKind.RSS,
            default=[RawItem(url="https://example.com/a", title="A", external_id="a")],
        )
    )
    registry.register(XFetcher(FakeTweetSource({"karpathy": FetchError("karpathy: x rate limit")})))

    try:
        summary = run_ingest(
            IngestServices(
                config=FilesystemConfigProvider(cfg),
                repository=repository,
                registry=registry,
                clock=_CLOCK,
            ),
            run_id="run-x",
        )
    finally:
        repository.close()

    by_id = {r.source_id: r for r in summary.results}
    assert by_id["good-rss"].status is FetchStatus.OK
    assert by_id["good-rss"].items_new == 1
    assert by_id["x-karpathy"].status is FetchStatus.ERROR
    assert "rate limit" in (by_id["x-karpathy"].error or "")
    assert summary.sources_error == 1  # the run completed; X did not abort it


def _stored_tweet(source_id: str, tweet_id: str) -> Item:
    return Item(
        item_id=f"{source_id}-{tweet_id}",
        source_id=source_id,
        kind=SourceKind.X,
        external_id=tweet_id,
        canonical_url=f"https://x.com/i/status/{tweet_id}",
        title="tweet",
        published_at="2026-07-08T00:00:00+00:00",
        fetched_at="2026-07-08T00:00:00+00:00",
        content_hash="h",
    )
