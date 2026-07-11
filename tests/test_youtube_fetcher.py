"""GRP-12: YouTube channel-RSS fetcher - no API key, yt:videoId as external_id."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from grepify.errors import FetchError
from grepify.ingest import TranscriptStore, YouTubeFetcher
from grepify.ingest.http import HttpResponse
from grepify.models import SourceKind
from grepify.paths import DataLayout
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


# --- T5 audit: bounded retry with backoff on transient 5xx -------------------


def test_transient_5xx_is_retried_then_succeeds() -> None:
    transport = ScriptedTransport(
        [
            HttpResponse(status_code=503, content=b"", headers={}),
            fixture_response("youtube", "channel.xml"),
        ]
    )
    sleeps: list[float] = []
    fetcher = YouTubeFetcher(transport, sleep=sleeps.append)
    source = make_source("s1", kind=SourceKind.YOUTUBE, url=_CHANNEL_URL)

    items = fetcher.fetch(source)

    assert len(items) == 2
    assert len(transport.calls) == 2  # one retry
    assert sleeps == [1.0]  # backoff before the retry, none after success


def test_5xx_retries_exhausted_raises_fetch_error() -> None:
    transport = ScriptedTransport(
        [
            HttpResponse(status_code=502, content=b"", headers={}),
            HttpResponse(status_code=503, content=b"", headers={}),
            HttpResponse(status_code=500, content=b"", headers={}),
        ]
    )
    sleeps: list[float] = []
    fetcher = YouTubeFetcher(transport, sleep=sleeps.append, max_attempts=3)
    source = make_source("s1", kind=SourceKind.YOUTUBE)

    with pytest.raises(FetchError, match="500"):
        fetcher.fetch(source)

    assert len(transport.calls) == 3  # bounded - not unbounded retry
    assert sleeps == [1.0, 2.0]  # exponential backoff, none after the final attempt


def test_max_attempts_below_one_rejected_eagerly() -> None:
    # Guards the backoff loop's invariant (it always runs >= 1 time) at
    # construction time, so a misconfigured caller gets a clear ValueError
    # instead of an assertion failing deep inside a retry loop.
    with pytest.raises(ValueError, match="max_attempts"):
        YouTubeFetcher(ScriptedTransport([]), max_attempts=0)


def test_4xx_is_not_retried() -> None:
    transport = ScriptedTransport([HttpResponse(status_code=403, content=b"", headers={})])
    sleeps: list[float] = []
    fetcher = YouTubeFetcher(transport, sleep=sleeps.append)
    source = make_source("s1", kind=SourceKind.YOUTUBE)

    with pytest.raises(FetchError, match="403"):
        fetcher.fetch(source)

    assert len(transport.calls) == 1  # no retry wasted on a hard client error
    assert sleeps == []


def test_malformed_feed_raises_fetch_error() -> None:
    transport = ScriptedTransport([fixture_response("rss", "malformed.xml")])
    with pytest.raises(FetchError, match="unparseable"):
        YouTubeFetcher(transport).fetch(make_source("s1", kind=SourceKind.YOUTUBE))


class _FakeTranscriptClient:
    def __init__(self, results: dict[str, str | None]) -> None:
        self.results = results

    def fetch(self, video_id: str, *, languages: Sequence[str]) -> str | None:
        return self.results.get(video_id)


def _transcript_store(tmp_path: Path, results: dict[str, str | None]) -> TranscriptStore:
    return TranscriptStore(
        DataLayout(tmp_path), _FakeTranscriptClient(results), max_chars=60000, languages=["en"]
    )


def test_transcript_ref_attached_when_store_present(tmp_path: Path) -> None:
    transport = ScriptedTransport([fixture_response("youtube", "channel.xml")])
    store = _transcript_store(tmp_path, {"vid0001AAA": "a transcript", "vid0002BBB": None})
    source = make_source("two-minute-papers", kind=SourceKind.YOUTUBE, url=_CHANNEL_URL)

    items = YouTubeFetcher(transport, transcript_store=store).fetch(source)

    # F-ING-03: present transcript attached; absent one leaves transcript_ref null.
    assert items[0].transcript_ref == "transcripts/vid0001AAA.txt.gz"
    assert items[1].transcript_ref is None


def test_no_transcript_refs_without_store() -> None:
    transport = ScriptedTransport([fixture_response("youtube", "channel.xml")])
    source = make_source("two-minute-papers", kind=SourceKind.YOUTUBE, url=_CHANNEL_URL)
    items = YouTubeFetcher(transport).fetch(source)
    assert all(i.transcript_ref is None for i in items)
