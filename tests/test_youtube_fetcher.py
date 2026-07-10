"""GRP-12: YouTube channel-RSS fetcher - no API key, yt:videoId as external_id."""

from __future__ import annotations

import pytest

from grepify.errors import FetchError
from grepify.ingest import YouTubeFetcher
from grepify.ingest.http import HttpResponse
from grepify.models import SourceKind
from tests.conftest import ScriptedTransport, fixture_response, make_source

_CHANNEL_URL = "https://www.youtube.com/feeds/videos.xml?channel_id=UC1234567890abcdefghijk"


def test_video_id_used_as_external_id() -> None:
    transport = ScriptedTransport([fixture_response("youtube", "channel.xml")])
    source = make_source("two-minute-papers", kind=SourceKind.YOUTUBE, url=_CHANNEL_URL)

    items = YouTubeFetcher(transport).fetch(source)

    assert len(items) == 2
    assert items[0].external_id == "vid0001AAA"
    assert items[1].external_id == "vid0002BBB"


def test_maps_title_url_author_summary_published() -> None:
    transport = ScriptedTransport([fixture_response("youtube", "channel.xml")])
    source = make_source("two-minute-papers", kind=SourceKind.YOUTUBE, url=_CHANNEL_URL)

    first = YouTubeFetcher(transport).fetch(source)[0]

    assert first.title == "This New AI Sees Around Corners!"
    assert first.url == "https://www.youtube.com/watch?v=vid0001AAA"
    assert first.author == "Two Minute Papers"
    assert first.summary == "A new non-line-of-sight imaging paper, explained."
    assert first.published_at == "2026-07-05T14:00:05+00:00"


def test_empty_channel_returns_empty_list() -> None:
    transport = ScriptedTransport([fixture_response("youtube", "empty.xml")])
    source = make_source("brand-new-channel", kind=SourceKind.YOUTUBE)
    assert YouTubeFetcher(transport).fetch(source) == []


def test_http_error_status_raises_fetch_error() -> None:
    transport = ScriptedTransport([HttpResponse(status_code=404, content=b"", headers={})])
    with pytest.raises(FetchError, match="404"):
        YouTubeFetcher(transport).fetch(make_source("s1", kind=SourceKind.YOUTUBE))


def test_transport_exception_becomes_fetch_error() -> None:
    transport = ScriptedTransport([ConnectionError("connection refused")])
    with pytest.raises(FetchError, match="s1"):
        YouTubeFetcher(transport).fetch(make_source("s1", kind=SourceKind.YOUTUBE))


def test_malformed_feed_raises_fetch_error() -> None:
    transport = ScriptedTransport([fixture_response("rss", "malformed.xml")])
    with pytest.raises(FetchError, match="unparseable"):
        YouTubeFetcher(transport).fetch(make_source("s1", kind=SourceKind.YOUTUBE))
