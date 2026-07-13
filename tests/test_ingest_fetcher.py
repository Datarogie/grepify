"""GRP-10: Fetcher interface, registry dispatch, and the fake fetcher."""

from __future__ import annotations

import pydantic
import pytest

from grepify.errors import FetchError
from grepify.ingest import FakeFetcher, FetcherRegistry, RawItem
from grepify.models import SourceKind
from tests.conftest import make_source


def _raw(external_id: str) -> RawItem:
    return RawItem(
        url=f"https://example.com/{external_id}", title=f"t {external_id}", external_id=external_id
    )


def test_registry_dispatches_by_source_kind() -> None:
    rss = FakeFetcher(SourceKind.RSS, default=[_raw("a"), _raw("b")])
    reddit = FakeFetcher(SourceKind.REDDIT, default=[_raw("z")])
    reg = FetcherRegistry()
    reg.register(rss)
    reg.register(reddit)

    assert reg.registered_kinds() == frozenset({SourceKind.RSS, SourceKind.REDDIT})
    assert reg.fetch(make_source("s1", kind=SourceKind.RSS)) == [_raw("a"), _raw("b")]
    assert reg.fetch(make_source("s2", kind=SourceKind.REDDIT)) == [_raw("z")]
    assert rss.calls == ["s1"]
    assert reddit.calls == ["s2"]


def test_registry_rejects_duplicate_kind() -> None:
    reg = FetcherRegistry()
    reg.register(FakeFetcher(SourceKind.RSS))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(FakeFetcher(SourceKind.RSS))


def test_registry_missing_kind_raises_keyerror() -> None:
    reg = FetcherRegistry()
    with pytest.raises(KeyError):
        reg.get(SourceKind.YOUTUBE)
    with pytest.raises(KeyError):
        reg.fetch(make_source("s1", kind=SourceKind.YOUTUBE))


def test_empty_feed_is_not_an_error() -> None:
    reg = FetcherRegistry()
    reg.register(FakeFetcher(SourceKind.RSS))  # no default -> empty
    assert reg.fetch(make_source("s1", kind=SourceKind.RSS)) == []


def test_fetch_error_propagates_for_orchestrator_isolation() -> None:
    boom = FetchError("timeout")
    fetcher = FakeFetcher(SourceKind.RSS, results={"dead": boom}, default=[_raw("ok")])
    reg = FetcherRegistry()
    reg.register(fetcher)

    # A per-source failure surfaces as FetchError (the orchestrator isolates it)...
    with pytest.raises(FetchError, match="timeout"):
        reg.fetch(make_source("dead", kind=SourceKind.RSS))
    # ...and does not poison other sources on the same fetcher.
    assert reg.fetch(make_source("live", kind=SourceKind.RSS)) == [_raw("ok")]
    assert fetcher.calls == ["dead", "live"]


def test_fetcher_kind_matches_registration_key() -> None:
    fetcher = FakeFetcher(SourceKind.X)
    reg = FetcherRegistry()
    reg.register(fetcher)
    assert reg.get(SourceKind.X) is fetcher
    assert fetcher.kind is SourceKind.X


def test_rawitem_forbids_unknown_fields() -> None:
    with pytest.raises(pydantic.ValidationError):
        RawItem(url="https://x", title="t", bogus="nope")  # type: ignore[call-arg]


def test_rawitem_requires_url_and_title() -> None:
    with pytest.raises(pydantic.ValidationError):
        RawItem(title="t")  # type: ignore[call-arg]
