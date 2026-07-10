"""GRP-53: transcript excerpts flow into extraction batches.

Checks that a youtube item's stored transcript is excerpted (<=1500 chars, smart
cut) into its extraction prompt payload and that the augmentation is additive:
non-youtube items, youtube items without a transcript, and the reader-less path
all keep the byte-identical v1 prompt.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from grepify.clock import FixedClock
from grepify.extract import build_messages, run_extract
from grepify.extract.prompt import _TRANSCRIPT_INTRO
from grepify.llm import LlmClient
from grepify.llm.client import RetryPolicy
from grepify.models import Item, SourceKind
from tests.conftest import FakeFallbackExtractor, ScriptedCompletionTransport, envelope_response

_CLOCK = FixedClock(datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC))


def _yt_item(item_id: str, *, transcript_ref: str | None) -> Item:
    return Item(
        item_id=item_id,
        source_id="yt-src",
        kind=SourceKind.YOUTUBE,
        external_id=item_id,
        canonical_url=f"https://www.youtube.com/watch?v={item_id}",
        title=f"Video {item_id}",
        summary="desc",
        published_at="2026-07-08T09:00:00+00:00",
        fetched_at="2026-07-08T10:00:00+00:00",
        content_hash="h",
        transcript_ref=transcript_ref,
    )


def _rss_item(item_id: str, *, transcript_ref: str | None = None) -> Item:
    return Item(
        item_id=item_id,
        source_id="rss-src",
        kind=SourceKind.RSS,
        external_id=item_id,
        canonical_url=f"https://example.com/{item_id}",
        title=f"Article {item_id}",
        summary="desc",
        published_at="2026-07-08T09:00:00+00:00",
        fetched_at="2026-07-08T10:00:00+00:00",
        content_hash="h",
        transcript_ref=transcript_ref,
    )


def _reader(mapping: dict[str, str]) -> Callable[[str], str | None]:
    return mapping.get


def test_transcript_excerpt_added_to_youtube_payload() -> None:
    long_transcript = "This is the opening of the talk. " + "detail " * 1000
    system, user = build_messages(
        [_yt_item("v1", transcript_ref="transcripts/v1.txt.gz")],
        max_keywords=8,
        transcript_reader=_reader({"transcripts/v1.txt.gz": long_transcript}),
    )
    payload = json.loads(user.content)
    assert "transcript" in payload[0]
    assert len(payload[0]["transcript"]) <= 1500
    assert payload[0]["transcript"].startswith("This is the opening")
    assert _TRANSCRIPT_INTRO.strip() in system.content


def test_no_transcript_field_for_non_youtube() -> None:
    _system, user = build_messages(
        [_rss_item("a", transcript_ref="transcripts/a.txt.gz")],
        max_keywords=8,
        transcript_reader=_reader({"transcripts/a.txt.gz": "should be ignored"}),
    )
    assert "transcript" not in json.loads(user.content)[0]


def test_no_transcript_field_when_ref_missing() -> None:
    _system, user = build_messages(
        [_yt_item("v1", transcript_ref=None)],
        max_keywords=8,
        transcript_reader=_reader({}),
    )
    assert "transcript" not in json.loads(user.content)[0]


def test_reader_yielding_nothing_keeps_v1_prompt() -> None:
    with_reader = build_messages(
        [_yt_item("v1", transcript_ref="transcripts/v1.txt.gz")],
        max_keywords=8,
        transcript_reader=_reader({}),  # blob unreadable -> None
    )
    without_reader = build_messages(
        [_yt_item("v1", transcript_ref="transcripts/v1.txt.gz")], max_keywords=8
    )
    assert with_reader[0].content == without_reader[0].content
    assert with_reader[1].content == without_reader[1].content
    assert _TRANSCRIPT_INTRO.strip() not in with_reader[0].content


def test_run_extract_forwards_reader_to_prompt() -> None:
    transport = ScriptedCompletionTransport(
        [envelope_response(json.dumps([{"item_id": "v1", "keywords": ["nanogpt"]}]))]
    )
    client = LlmClient(
        model="test-model",
        base_url="https://x/v1",
        api_key="k",
        log_sink=lambda _e: None,
        clock=_CLOCK,
        transport=transport,
        retry=RetryPolicy(sleep=lambda _s: None, rng=lambda: 0.0),
    )
    run_extract(
        [_yt_item("v1", transcript_ref="transcripts/v1.txt.gz")],
        client,
        run_id="r1",
        clock=_CLOCK,
        fallback=FakeFallbackExtractor(),
        transcript_reader=_reader({"transcripts/v1.txt.gz": "the transcript body"}),
    )
    (_url, _headers, payload) = transport.posts[0]
    user_message = payload["messages"][1]["content"]
    assert "the transcript body" in user_message
