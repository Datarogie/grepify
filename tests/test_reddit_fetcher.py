"""GRP-13: Reddit fetcher - new.json with UA + backoff, 50-item cap, .rss fallback."""

from __future__ import annotations

import json

import pytest

from grepify.errors import FetchError
from grepify.ingest import RedditFetcher
from grepify.ingest.http import HttpResponse
from grepify.models import Rung, SourceKind
from tests.conftest import ScriptedTransport, fixture_response, make_source

_SUB_URL = "https://www.reddit.com/r/LocalLLaMA/new.json"


def _fetcher(transport: ScriptedTransport, **kw: object) -> RedditFetcher:
    return RedditFetcher(transport, sleep=lambda _seconds: None, **kw)  # type: ignore[arg-type]


def test_maps_title_summary_author_published_and_permalink_url() -> None:
    transport = ScriptedTransport([fixture_response("reddit", "new.json")])
    source = make_source("localllama", kind=SourceKind.REDDIT, url=_SUB_URL)

    items = _fetcher(transport).fetch(source)

    assert len(items) == 3
    first = items[0]
    assert first.title == "Anyone else running a 70B model locally now?"
    assert first.external_id == "abc001"
    assert first.author == "quant_curious"
    assert first.summary is not None and first.summary.startswith("Got a used 3090")
    assert (
        first.url
        == "https://www.reddit.com/r/LocalLLaMA/comments/abc001/anyone_else_running_a_70b_model_locally_now/"
    )
    assert first.published_at == "2026-07-08T06:00:00+00:00"


def test_link_post_with_empty_selftext_has_none_summary() -> None:
    transport = ScriptedTransport([fixture_response("reddit", "new.json")])
    items = _fetcher(transport).fetch(make_source("localllama", url=_SUB_URL))
    assert items[1].summary is None


def test_requests_limit_50() -> None:
    transport = ScriptedTransport([fixture_response("reddit", "new.json")])
    _fetcher(transport).fetch(make_source("localllama", url=_SUB_URL))
    url, _headers = transport.calls[0]
    assert "limit=50" in url


def test_sends_descriptive_user_agent() -> None:
    transport = ScriptedTransport([fixture_response("reddit", "new.json")])
    _fetcher(transport).fetch(make_source("localllama", url=_SUB_URL))
    _url, headers = transport.calls[0]
    assert "grepify" in headers["user-agent"]


def test_empty_listing_returns_empty_list() -> None:
    transport = ScriptedTransport([fixture_response("reddit", "empty.json")])
    assert _fetcher(transport).fetch(make_source("localllama", url=_SUB_URL)) == []


def test_item_cap_enforced_even_if_server_ignores_limit() -> None:
    # A response with more than 50 children still gets truncated client-side.
    payload = {
        "kind": "Listing",
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": f"id{i}",
                        "title": f"post {i}",
                        "selftext": "",
                        "author": "u",
                        "permalink": f"/r/x/comments/id{i}/post_{i}/",
                        "created_utc": 1751980800.0,
                    },
                }
                for i in range(75)
            ]
        },
    }
    transport = ScriptedTransport(
        [HttpResponse(status_code=200, content=json.dumps(payload).encode(), headers={})]
    )
    items = _fetcher(transport).fetch(make_source("localllama", url=_SUB_URL))
    assert len(items) == 50


def test_malformed_json_raises_fetch_error() -> None:
    transport = ScriptedTransport([fixture_response("reddit", "malformed.json")])
    with pytest.raises(FetchError, match="malformed reddit json"):
        _fetcher(transport).fetch(make_source("localllama", url=_SUB_URL))


def test_child_missing_data_key_raises_fetch_error_not_keyerror() -> None:
    payload = {"kind": "Listing", "data": {"children": [{"kind": "t3"}]}}
    transport = ScriptedTransport(
        [HttpResponse(status_code=200, content=json.dumps(payload).encode(), headers={})]
    )
    with pytest.raises(FetchError, match="malformed reddit json"):
        _fetcher(transport).fetch(make_source("localllama", url=_SUB_URL))


def test_non_numeric_created_utc_raises_fetch_error_not_typeerror() -> None:
    payload = {
        "kind": "Listing",
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": "id1",
                        "title": "post",
                        "permalink": "/r/x/comments/id1/post/",
                        "created_utc": "not-a-number",
                    },
                }
            ]
        },
    }
    transport = ScriptedTransport(
        [HttpResponse(status_code=200, content=json.dumps(payload).encode(), headers={})]
    )
    with pytest.raises(FetchError, match="malformed reddit json"):
        _fetcher(transport).fetch(make_source("localllama", url=_SUB_URL))


def test_link_post_without_permalink_falls_back_to_outbound_url() -> None:
    payload = {
        "kind": "Listing",
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": "id1",
                        "title": "post",
                        "url": "https://example.com/outbound-article",
                        "created_utc": 1783490400.0,
                    },
                }
            ]
        },
    }
    transport = ScriptedTransport(
        [HttpResponse(status_code=200, content=json.dumps(payload).encode(), headers={})]
    )
    items = _fetcher(transport).fetch(make_source("localllama", url=_SUB_URL))
    assert items[0].url == "https://example.com/outbound-article"


def test_retries_on_429_then_succeeds() -> None:
    transport = ScriptedTransport(
        [
            HttpResponse(status_code=429, content=b"", headers={}),
            fixture_response("reddit", "new.json"),
        ]
    )
    items = _fetcher(transport, max_attempts=3).fetch(make_source("localllama", url=_SUB_URL))
    assert len(items) == 3
    assert len(transport.calls) == 2


def test_sleeps_with_exponential_backoff_between_retries() -> None:
    sleeps: list[float] = []
    transport = ScriptedTransport(
        [
            HttpResponse(status_code=503, content=b"", headers={}),
            HttpResponse(status_code=503, content=b"", headers={}),
            fixture_response("reddit", "new.json"),
        ]
    )
    fetcher = RedditFetcher(transport, sleep=sleeps.append, max_attempts=3)
    fetcher.fetch(make_source("localllama", url=_SUB_URL))
    assert sleeps == [1.0, 2.0]


def test_non_retryable_403_falls_back_to_rss_immediately() -> None:
    transport = ScriptedTransport(
        [
            HttpResponse(status_code=403, content=b"", headers={}),
            fixture_response("reddit", "fallback.rss"),
        ]
    )
    items = _fetcher(transport).fetch(make_source("localllama", url=_SUB_URL))

    assert len(transport.calls) == 2  # no retries wasted on the 403
    assert transport.calls[1][0] == "https://www.reddit.com/r/LocalLLaMA/new.rss"
    assert len(items) == 2
    assert items[0].external_id == "t3_abc001"
    assert items[0].title == "Anyone else running a 70B model locally now?"


def test_retries_exhausted_then_falls_back_to_rss() -> None:
    transport = ScriptedTransport(
        [
            HttpResponse(status_code=503, content=b"", headers={}),
            HttpResponse(status_code=503, content=b"", headers={}),
            fixture_response("reddit", "fallback.rss"),
        ]
    )
    items = _fetcher(transport, max_attempts=2).fetch(make_source("localllama", url=_SUB_URL))
    assert len(transport.calls) == 3  # 2 json attempts + 1 rss fallback
    assert len(items) == 2


def test_json_and_rss_fallback_both_failing_raises_fetch_error() -> None:
    transport = ScriptedTransport(
        [
            HttpResponse(status_code=403, content=b"", headers={}),
            HttpResponse(status_code=503, content=b"", headers={}),
        ]
    )
    with pytest.raises(FetchError, match="fallback"):
        _fetcher(transport).fetch(make_source("localllama", url=_SUB_URL))


def test_transport_exception_retried_then_falls_back() -> None:
    transport = ScriptedTransport(
        [
            TimeoutError("timed out"),
            fixture_response("reddit", "fallback.rss"),
        ]
    )
    items = _fetcher(transport, max_attempts=1).fetch(make_source("localllama", url=_SUB_URL))
    assert len(items) == 2


# --- acquisition-rung reporting (ADR 0002 §1, GRP-66) ------------------------


def test_acquire_reports_direct_rung_on_json_success() -> None:
    transport = ScriptedTransport([fixture_response("reddit", "new.json")])
    outcome = _fetcher(transport).acquire(
        make_source("localllama", kind=SourceKind.REDDIT, url=_SUB_URL)
    )
    assert outcome.rung is Rung.DIRECT
    assert outcome.resolved_url is None
    assert len(outcome.items) == 3


def test_acquire_reports_alt_endpoint_rung_on_rss_fallback() -> None:
    transport = ScriptedTransport(
        [
            HttpResponse(status_code=403, content=b"", headers={}),
            fixture_response("reddit", "fallback.rss"),
        ]
    )
    outcome = _fetcher(transport).acquire(
        make_source("localllama", kind=SourceKind.REDDIT, url=_SUB_URL)
    )
    assert outcome.rung is Rung.ALT_ENDPOINT
    assert outcome.resolved_url == "https://www.reddit.com/r/LocalLLaMA/new.rss"
    assert len(outcome.items) == 2


def test_json_429_retry_after_is_respected_and_traced() -> None:
    sleeps: list[float] = []
    transport = ScriptedTransport(
        [
            HttpResponse(status_code=429, content=b"", headers={"retry-after": "7"}),
            fixture_response("reddit", "fallback.rss"),
        ]
    )
    outcome = RedditFetcher(transport, sleep=sleeps.append, max_attempts=1).acquire(
        make_source("localllama", kind=SourceKind.REDDIT, url=_SUB_URL)
    )
    assert outcome.rung is Rung.ALT_ENDPOINT
    assert sleeps == []
    assert outcome.acquisition_trace is not None
    assert "rate_limited" in outcome.acquisition_trace
    assert '"retry_after":"7"' in outcome.acquisition_trace


def test_json_429_retry_after_bounds_the_sleep_between_attempts() -> None:
    sleeps: list[float] = []
    transport = ScriptedTransport(
        [
            HttpResponse(status_code=429, content=b"", headers={"retry-after": "7"}),
            HttpResponse(status_code=429, content=b"", headers={"retry-after": "not-a-number"}),
            fixture_response("reddit", "fallback.rss"),
        ]
    )
    outcome = RedditFetcher(transport, sleep=sleeps.append, max_attempts=2).acquire(
        make_source("localllama", kind=SourceKind.REDDIT, url=_SUB_URL)
    )
    assert outcome.rung is Rung.ALT_ENDPOINT
    assert sleeps == [7.0]
    assert outcome.acquisition_trace is not None
    assert '"retry_after":"unusable"' in outcome.acquisition_trace
    assert "not-a-number" not in outcome.acquisition_trace


def test_json_hostile_retry_after_never_stalls_the_run() -> None:
    sleeps: list[float] = []
    transport = ScriptedTransport(
        [
            HttpResponse(status_code=429, content=b"", headers={"retry-after": "86400"}),
            HttpResponse(status_code=429, content=b"", headers={}),
            fixture_response("reddit", "fallback.rss"),
        ]
    )
    outcome = RedditFetcher(transport, sleep=sleeps.append, max_attempts=2).acquire(
        make_source("localllama", kind=SourceKind.REDDIT, url=_SUB_URL)
    )
    assert outcome.rung is Rung.ALT_ENDPOINT
    assert sleeps == [1.0]
    assert outcome.acquisition_trace is not None
    assert '"retry_after":"unusable"' in outcome.acquisition_trace
    assert '"retry_after":"absent"' in outcome.acquisition_trace
