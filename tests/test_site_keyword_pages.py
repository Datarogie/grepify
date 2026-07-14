"""Keyword detail dataset tests (GRP-44, F-TRD-02): threshold, timeline, co-occurrence.

Drives :meth:`TrendQueries.keyword_details` on a canned cache - the AC's
"co-occurrence query unit-tested" - plus the daily timeline, distinct sources,
latest-by-kind grouping, alias/mute folding, and the >= min_mentions threshold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from grepify.clock import from_iso, to_iso
from grepify.config.schemas import KeywordsConfig
from grepify.digest.periods import edmonton_date
from grepify.keywords import KeywordRules
from grepify.models import ExtractionMethod, Item, ItemKeyword, Source, SourceKind
from grepify.paths import DataLayout
from grepify.repository import JsonlSqliteRepository
from grepify.site.trends import TrendQueries, open_cache, window_ending_at

# 07-08 07:00 MDT -> 30d window ends at Edmonton midnight: [2026-06-08, 2026-07-08)
_NOW = datetime(2026, 7, 8, 13, 0, tzinfo=UTC)


def _item(item_id: str, *, kind: SourceKind, source_id: str, published_at: str) -> Item:
    return Item(
        item_id=item_id,
        source_id=source_id,
        kind=kind,
        external_id=item_id,
        canonical_url=f"https://example.com/{item_id}",
        title=f"title {item_id}",
        summary="s",
        published_at=published_at,
        fetched_at="2026-07-08T00:00:00+00:00",
        content_hash=f"hash-{item_id}",
    )


def _kw(item_id: str, keyword: str, rank: int = 1) -> ItemKeyword:
    return ItemKeyword(
        item_id=item_id,
        keyword=keyword,
        rank=rank,
        method=ExtractionMethod.LLM,
        model="m",
        extracted_at="2026-07-08T00:00:00+00:00",
    )


def _source(source_id: str) -> Source:
    return Source(
        source_id=source_id,
        name=source_id.upper(),
        kind=SourceKind.RSS,
        url=f"https://example.com/{source_id}/feed",
        url_hash=f"urlhash-{source_id}",
        group_id="g",
        added_at="2026-07-01T00:00:00+00:00",
    )


def _queries(tmp_path: Path) -> TrendQueries:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items(
        [
            _item(
                "i1", kind=SourceKind.RSS, source_id="s1", published_at="2026-07-01T10:00:00+00:00"
            ),
            _item(
                "i2",
                kind=SourceKind.YOUTUBE,
                source_id="s2",
                published_at="2026-07-02T10:00:00+00:00",
            ),
            _item(
                "i3", kind=SourceKind.RSS, source_id="s1", published_at="2026-07-03T10:00:00+00:00"
            ),
            _item(
                "old", kind=SourceKind.RSS, source_id="s1", published_at="2026-01-01T10:00:00+00:00"
            ),
        ]
    )
    repo.add_item_keywords(
        [
            _kw("i1", "genai"),
            _kw("i1", "agents", rank=2),
            _kw("i1", "webinar", rank=3),  # muted
            _kw("i2", "gen ai"),
            _kw("i2", "llm", rank=2),  # "gen ai" aliases to genai
            _kw("i3", "genai"),
            _kw("i3", "agents", rank=2),
            _kw("old", "genai"),  # outside the 30d window
        ]
    )
    repo.load_config([], [_source("s1"), _source("s2")])
    repo.rebuild_cache()
    repo.close()
    rules = KeywordRules.from_config(KeywordsConfig(aliases={"gen ai": "genai"}, mute=["webinar"]))
    return TrendQueries(open_cache(DataLayout(tmp_path)), rules)


def test_only_keywords_above_threshold_get_a_detail(tmp_path: Path) -> None:
    details = _queries(tmp_path).keyword_details(window_ending_at(_NOW, days=30), min_mentions=3)
    # genai: i1,i2 (aliased),i3 = 3 (>=3). agents: i1,i3 = 2 (<3). llm: 1. webinar: muted.
    assert set(details) == {"genai"}


def test_co_occurrence_is_count_ranked(tmp_path: Path) -> None:
    details = _queries(tmp_path).keyword_details(window_ending_at(_NOW, days=30), min_mentions=1)
    genai = details["genai"]
    assert genai.count == 3
    assert genai.source_count == 2  # s1, s2
    # agents co-occurs on i1 and i3 (2); llm co-occurs on i2 (1); webinar muted out
    assert [(c.keyword, c.count) for c in genai.co_occurring] == [("agents", 2), ("llm", 1)]


def test_timeline_buckets_by_day(tmp_path: Path) -> None:
    details = _queries(tmp_path).keyword_details(window_ending_at(_NOW, days=30), min_mentions=1)
    timeline = details["genai"].timeline
    assert len(timeline) == 30
    assert sum(timeline) == 3  # one mention on each of 3 distinct days
    assert max(timeline) == 1


def test_latest_content_grouped_by_kind(tmp_path: Path) -> None:
    details = _queries(tmp_path).keyword_details(window_ending_at(_NOW, days=30), min_mentions=1)
    genai = details["genai"]
    assert genai.kinds == ["rss", "youtube"]  # sorted tab order
    assert [i.item_id for i in genai.items_by_kind["rss"]] == ["i3", "i1"]  # newest first
    assert [i.item_id for i in genai.items_by_kind["youtube"]] == ["i2"]


def test_keyword_details_deterministic(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    window = window_ending_at(_NOW, days=30)
    assert q.keyword_details(window, min_mentions=1) == q.keyword_details(window, min_mentions=1)


def test_timeline_late_evening_item_is_not_clamped(tmp_path: Path) -> None:
    # An item at 2026-07-07T05:00Z is 2026-07-06 23:00 in Edmonton, so it belongs
    # to the 07-06 bucket, not the final (07-07) one. The old UTC-date logic read
    # its date as 07-07 and clamped it into the edge bucket; the aligned logic
    # keeps it distinct.
    def _rss(item_id: str, published_at: str) -> Item:
        return _item(item_id, kind=SourceKind.RSS, source_id="s1", published_at=published_at)

    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items(
        [
            _rss("a", "2026-07-06T20:00:00+00:00"),
            _rss("b", "2026-07-07T05:00:00+00:00"),
            _rss("c", "2026-07-07T10:00:00+00:00"),
        ]
    )
    repo.add_item_keywords([_kw("a", "k"), _kw("b", "k"), _kw("c", "k")])
    repo.load_config([], [_source("s1")])
    repo.rebuild_cache()
    repo.close()
    rules = KeywordRules.from_config(KeywordsConfig(aliases={}, mute=[]))
    q = TrendQueries(open_cache(DataLayout(tmp_path)), rules)

    timeline = q.keyword_details(window_ending_at(_NOW, days=7), min_mentions=1)["k"].timeline
    # start date is Edmonton 2026-07-01; a/b -> 07-06 (index 5), c -> 07-07 (index 6)
    assert len(timeline) == 7
    assert sum(timeline) == 3
    assert timeline[5] == 2
    assert timeline[6] == 1


def test_timeline_property_every_in_window_item_maps_to_one_bucket(tmp_path: Path) -> None:
    # Property (GRP-71 DoD): across a dense grid of timestamps spanning the whole
    # window, every in-window item lands in exactly one bucket keyed by its
    # Edmonton day - no drops, no double counts, no clamped overflow.
    window = window_ending_at(_NOW, days=7)
    start = from_iso(window.start)
    stamps = [start + timedelta(hours=h) for h in range(7 * 24)]  # one per hour

    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items(
        [
            _item(f"h{i}", kind=SourceKind.RSS, source_id="s1", published_at=to_iso(ts))
            for i, ts in enumerate(stamps)
        ]
    )
    repo.add_item_keywords([_kw(f"h{i}", "k") for i in range(len(stamps))])
    repo.load_config([], [_source("s1")])
    repo.rebuild_cache()
    repo.close()
    rules = KeywordRules.from_config(KeywordsConfig(aliases={}, mute=[]))
    q = TrendQueries(open_cache(DataLayout(tmp_path)), rules)

    timeline = q.keyword_details(window, min_mentions=1)["k"].timeline

    start_day = edmonton_date(from_iso(window.start))
    expected = [0] * 7
    for ts in stamps:
        expected[(edmonton_date(ts) - start_day).days] += 1
    assert timeline == expected
    assert sum(timeline) == len(stamps)  # every item counted exactly once
