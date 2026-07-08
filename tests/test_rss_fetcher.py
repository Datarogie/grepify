"""GRP-11: RSS fetcher — conditional GET, timeout, malformed-feed tolerance."""

from __future__ import annotations

import pytest

from grepify.errors import FetchError
from grepify.ingest import RssFetcher
from grepify.ingest.http import HttpResponse
from tests.conftest import ScriptedTransport, fixture_response, make_source


def test_valid_feed_maps_all_entries() -> None:
    transport = ScriptedTransport([fixture_response("rss", "valid.xml")])
    fetcher = RssFetcher(transport)
    source = make_source("ahead-of-ai", url="https://example.com/ahead-of-ai/feed")

    items = fetcher.fetch(source)

    assert len(items) == 3
    first = items[0]
    assert first.title == "Scaling Laws Revisited"
    assert first.url == "https://example.com/ahead-of-ai/scaling-laws-revisited"
    assert first.external_id == "urn:uuid:1111-scaling-laws"
    assert first.summary is not None and "scaling laws" in first.summary.lower()
    assert first.author is not None and "Sebastian" in first.author
    assert first.published_at == "2026-07-06T09:00:00+00:00"
    assert first.lang == "en-us"


def test_sends_user_agent_header() -> None:
    transport = ScriptedTransport([fixture_response("rss", "empty.xml")])
    fetcher = RssFetcher(transport)
    fetcher.fetch(make_source("s1"))
    _url, headers = transport.calls[0]
    assert "user-agent" in headers
    assert "grepify" in headers["user-agent"]


def test_bad_dates_fall_back_to_none() -> None:
    transport = ScriptedTransport([fixture_response("rss", "bad_dates.xml")])
    items = RssFetcher(transport).fetch(make_source("data-eng-roundup"))

    assert len(items) == 3
    assert items[0].published_at is None  # "whenever, probably last week"
    assert items[1].published_at is None  # garbage date string
    assert items[2].published_at == "2026-07-09T08:30:00+00:00"  # well-formed


def test_missing_guid_external_id_is_none() -> None:
    transport = ScriptedTransport([fixture_response("rss", "missing_guids.xml")])
    items = RssFetcher(transport).fetch(make_source("infoworld-ai"))

    assert len(items) == 2
    assert all(item.external_id is None for item in items)
    assert items[0].url == "https://example.com/infoworld-ai/vector-db-checklist"


def test_html_in_title_is_cleaned() -> None:
    transport = ScriptedTransport([fixture_response("rss", "html_in_title.xml")])
    items = RssFetcher(transport).fetch(make_source("ai-business"))

    assert len(items) == 2
    assert "<" not in items[0].title and ">" not in items[0].title
    assert "Breaking" in items[0].title and "Anthropic & Google" in items[0].title
    assert "<" not in items[1].title
    assert "Down 4%" in items[1].title


def test_empty_feed_returns_empty_list() -> None:
    transport = ScriptedTransport([fixture_response("rss", "empty.xml")])
    assert RssFetcher(transport).fetch(make_source("nvidia-blog")) == []


def test_totally_malformed_feed_raises_fetch_error() -> None:
    transport = ScriptedTransport([fixture_response("rss", "malformed.xml")])
    with pytest.raises(FetchError, match="unparseable"):
        RssFetcher(transport).fetch(make_source("dead-feed"))


def test_http_error_status_raises_fetch_error() -> None:
    transport = ScriptedTransport([HttpResponse(status_code=503, content=b"", headers={})])
    with pytest.raises(FetchError, match="503"):
        RssFetcher(transport).fetch(make_source("s1"))


def test_transport_exception_becomes_fetch_error() -> None:
    transport = ScriptedTransport([TimeoutError("connect timed out")])
    with pytest.raises(FetchError, match="s1"):
        RssFetcher(transport).fetch(make_source("s1"))


def test_conditional_get_sends_etag_and_last_modified_on_next_call() -> None:
    transport = ScriptedTransport(
        [
            fixture_response(
                "rss",
                "valid.xml",
                headers={"etag": '"v1"', "last-modified": "Wed, 08 Jul 2026 00:00:00 GMT"},
            )
        ]
    )
    fetcher = RssFetcher(transport)
    source = make_source("ahead-of-ai")
    fetcher.fetch(source)  # populates the conditional-GET cache

    transport.calls.clear()
    transport.script.append(HttpResponse(status_code=304, content=b"", headers={}))
    second = fetcher.fetch(source)

    assert second == []
    _url, headers = transport.calls[0]
    assert headers["if-none-match"] == '"v1"'
    assert headers["if-modified-since"] == "Wed, 08 Jul 2026 00:00:00 GMT"


def test_conditional_get_cache_is_per_source() -> None:
    transport = ScriptedTransport(
        [
            fixture_response("rss", "valid.xml", headers={"etag": '"a"'}),
            fixture_response("rss", "empty.xml"),
        ]
    )
    fetcher = RssFetcher(transport)
    fetcher.fetch(make_source("s1"))
    fetcher.fetch(make_source("s2"))  # different source_id -> no cached etag sent

    _url, headers = transport.calls[1]
    assert "if-none-match" not in headers
