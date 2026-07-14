"""GRP-11: RSS fetcher - conditional GET, timeout, malformed-feed tolerance."""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from grepify.errors import FetchError
from grepify.ingest import RssFetcher
from grepify.ingest.http import HttpResponse, HttpxTransport, OutboundHttpClient
from grepify.models import Rung
from tests.conftest import ScriptedTransport, fixture_response, make_source

_FEED_URL = "https://example.com/s1/feed"


def _fail(status: int = 404) -> HttpResponse:
    return HttpResponse(status_code=status, content=b"", headers={})


def _html(body: bytes) -> HttpResponse:
    return HttpResponse(status_code=200, content=body, headers={})


_HTML_WITH_LINK = b'<link rel="alternate" type="application/rss+xml" href="/found.xml">'


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
    # A realistic browser UA - WAF-fronted feeds (Cloudflare/Substack) 403 a bot UA.
    assert headers["user-agent"].startswith("Mozilla/5.0")
    assert "Chrome" in headers["user-agent"]


def test_sends_accept_header() -> None:
    transport = ScriptedTransport([fixture_response("rss", "empty.xml")])
    fetcher = RssFetcher(transport)
    fetcher.fetch(make_source("s1"))
    _url, headers = transport.calls[0]
    assert "accept" in headers
    accept = headers["accept"]
    assert "application/rss+xml" in accept
    assert "application/atom+xml" in accept
    assert "xml" in accept


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


# --- acquisition ladder (ADR 0002 §1, GRP-66) --------------------------------


def test_acquire_direct_success_reports_direct_rung() -> None:
    transport = ScriptedTransport([fixture_response("rss", "valid.xml")])
    outcome = RssFetcher(transport).acquire(make_source("s1", url=_FEED_URL))
    assert outcome.rung is Rung.DIRECT
    assert outcome.resolved_url is None
    assert len(outcome.items) == 3
    assert len(transport.calls) == 1  # no fallback GET when rung 0 serves


def test_acquire_empty_feed_serves_directly_not_degraded() -> None:
    # A quiet (empty) feed is a successful rung-0 serve, not a failure to
    # escalate: the ladder must stop, still reporting DIRECT.
    transport = ScriptedTransport([fixture_response("rss", "empty.xml")])
    outcome = RssFetcher(transport).acquire(make_source("s1", url=_FEED_URL))
    assert outcome.rung is Rung.DIRECT
    assert outcome.items == []
    assert len(transport.calls) == 1


def test_acquire_falls_back_to_alt_endpoint() -> None:
    transport = ScriptedTransport([_fail(403), fixture_response("rss", "valid.xml")])
    outcome = RssFetcher(transport).acquire(make_source("s1", url=_FEED_URL))
    assert outcome.rung is Rung.ALT_ENDPOINT
    assert outcome.resolved_url == "https://example.com/s1/feed/"
    assert len(outcome.items) == 3
    assert len(transport.calls) == 2


def test_acquire_falls_back_to_autodiscovery() -> None:
    transport = ScriptedTransport(
        [
            _fail(),
            _fail(),
            _fail(),
            _fail(),
            _html(_HTML_WITH_LINK),
            fixture_response("rss", "valid.xml"),
        ]
    )
    outcome = RssFetcher(transport).acquire(make_source("s1", url=_FEED_URL))
    assert outcome.rung is Rung.AUTODISCOVERY
    assert outcome.resolved_url == "https://example.com/found.xml"
    assert len(outcome.items) == 3
    # 4 static rungs (direct + 3 alts) + 1 home-page GET + 1 discovered-feed GET.
    assert len(transport.calls) == 6
    assert transport.calls[4][0] == "https://example.com/"


def test_acquire_falls_back_to_pinned_mirror_after_autodiscovery() -> None:
    source = make_source("s1", url=_FEED_URL).model_copy(
        update={"active_url": "https://example.com/mirror.xml"}
    )
    # All statics fail; the home page has no feed link, so autodiscovery yields
    # nothing and the ADR rung-3 pinned mirror serves last.
    transport = ScriptedTransport(
        [
            _fail(),
            _fail(),
            _fail(),
            _fail(),
            _html(b"<html>no feed</html>"),
            fixture_response("rss", "valid.xml"),
        ]
    )
    outcome = RssFetcher(transport).acquire(source)
    assert outcome.rung is Rung.MIRROR
    assert outcome.resolved_url == "https://example.com/mirror.xml"
    assert transport.calls[-1][0] == "https://example.com/mirror.xml"


def test_acquire_all_rungs_failing_raises_with_bounded_attempts() -> None:
    # direct + 3 alts + 1 home-page probe = 5 GETs, then it stops (no active_url,
    # no discovered feed): bounded, never spins (PRD §9).
    transport = ScriptedTransport(
        [_fail(), _fail(), _fail(), _fail(), _html(b"<html>no feed</html>")]
    )
    with pytest.raises(FetchError, match="all acquisition rungs failed"):
        RssFetcher(transport).acquire(make_source("s1", url=_FEED_URL))
    assert len(transport.calls) == 5


def test_fetch_still_uses_only_the_direct_rung() -> None:
    # The single-rung fetch() path is unchanged for non-orchestrator callers:
    # a rung-0 failure raises, it never walks the ladder.
    transport = ScriptedTransport([_fail(403)])
    with pytest.raises(FetchError, match="403"):
        RssFetcher(transport).fetch(make_source("s1", url=_FEED_URL))
    assert len(transport.calls) == 1


def test_autodiscovery_status_error_redacts_sensitive_query() -> None:
    source = make_source("s1", url="https://example.com/feed?token=secret")
    fetcher = RssFetcher(ScriptedTransport([_fail(), _fail(), _fail()]))

    with pytest.raises(FetchError) as exc:
        fetcher.acquire(source)

    text = str(exc.value)
    assert "secret" not in text


def test_mirror_rung_uses_transport_policy_before_request() -> None:
    sent: list[str] = []
    fetcher = RssFetcher(
        HttpxTransport(
            client=OutboundHttpClient(
                resolver=lambda host, port: [ipaddress.ip_address("8.8.8.8")],
                transport_factory=lambda _: httpx.MockTransport(
                    lambda request: sent.append(str(request.url)) or httpx.Response(404)
                ),
            )
        )
    )
    source = make_source("s1", url="https://example.com/feed").model_copy(
        update={"active_url": "https://127.0.0.1/feed"}
    )

    with pytest.raises(FetchError):
        fetcher.acquire(source)

    assert sent == [
        "https://example.com/feed",
        "https://example.com/feed/",
        "https://example.com/feed/atom/",
        "https://example.com/?feed=rss2",
        "https://example.com/",
    ]


def test_substack_direct_success_stays_direct_without_generic_wordpress_alts() -> None:
    transport = ScriptedTransport([fixture_response("rss", "valid.xml")])
    source = make_source("getdbt-roundup", url="https://roundup.getdbt.com/feed")
    outcome = RssFetcher(transport).acquire(source)
    assert outcome.rung is Rung.DIRECT
    assert transport.calls == [("https://roundup.getdbt.com/feed", transport.calls[0][1])]


def test_substack_403_uses_explicit_pinned_fallback_and_preserves_identity() -> None:
    source = make_source("benn-substack", url="https://benn.substack.com/feed").model_copy(
        update={"active_url": "https://substack.com/feed/@benn"}
    )
    transport = ScriptedTransport(
        [
            _fail(403),
            _html(b"<html>no feed</html>"),
            fixture_response("rss", "valid.xml"),
        ]
    )
    outcome = RssFetcher(transport).acquire(source)
    assert outcome.rung is Rung.MIRROR
    assert outcome.resolved_url == "https://substack.com/feed/@benn"
    assert source.url == "https://benn.substack.com/feed"
    assert [call[0] for call in transport.calls] == [
        "https://benn.substack.com/feed",
        "https://benn.substack.com/",
        "https://substack.com/feed/@benn",
    ]
    assert outcome.acquisition_trace is not None
    assert "403" not in outcome.acquisition_trace
    assert "runner_blocked_or_forbidden" in outcome.acquisition_trace


def test_substack_all_methods_failing_is_bounded() -> None:
    source = make_source("benn-substack", url="https://benn.substack.com/feed").model_copy(
        update={"active_url": "https://substack.com/feed/@benn"}
    )
    transport = ScriptedTransport([_fail(403), _html(b"<html>no feed</html>"), _fail(403)])
    with pytest.raises(FetchError, match="all acquisition rungs failed"):
        RssFetcher(transport).acquire(source)
    assert len(transport.calls) == 3


def test_oversized_feed_rejected_before_parse() -> None:
    transport = ScriptedTransport(
        [HttpResponse(status_code=200, content=b"x" * 2_000_001, headers={})]
    )
    with pytest.raises(FetchError, match="too large"):
        RssFetcher(transport).fetch(make_source("s1"))


def test_acquisition_trace_redacts_sensitive_query_values() -> None:
    source = make_source("s1", url="https://example.com/feed?token=secret").model_copy(
        update={"active_url": "https://example.com/mirror.xml?api_key=secret"}
    )
    transport = ScriptedTransport(
        [
            _fail(403),
            _fail(404),
            _fail(404),
            _fail(404),
            _html(b"no"),
            fixture_response("rss", "valid.xml"),
        ]
    )
    outcome = RssFetcher(transport).acquire(source)
    assert outcome.acquisition_trace is not None
    assert "secret" not in outcome.acquisition_trace
    assert "REDACTED" in outcome.acquisition_trace
