"""GRP-51: X items in the site - kind tab + keyword extraction on tweet text.

The site is kind-generic (the keyword page tabs and the items-browser kind
filter are built from whatever ``item.kind`` values are present), so an X item
flows through with no template special-casing. This test proves that end to end
on the trend-query surface: an ``x`` item, whose ``title`` carries the tweet text
and which has an extracted keyword, appears under its own ``x`` tab alongside the
other kinds, and its tweet text is what the site shows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from grepify.config.schemas import KeywordsConfig
from grepify.ingest import FakeTweetSource, Tweet, XFetcher, normalize_batch
from grepify.keywords import KeywordRules
from grepify.models import ExtractionMethod, Item, ItemKeyword, Source, SourceKind
from grepify.paths import DataLayout
from grepify.repository import JsonlSqliteRepository
from grepify.site.trends import TrendQueries, open_cache, window_ending_at
from tests.conftest import make_source

_NOW = datetime(2026, 7, 9, tzinfo=UTC)


def _kw(item_id: str, keyword: str) -> ItemKeyword:
    return ItemKeyword(
        item_id=item_id,
        keyword=keyword,
        rank=1,
        method=ExtractionMethod.LLM,
        model="m",
        extracted_at="2026-07-08T00:00:00+00:00",
    )


def _source(source_id: str, kind: SourceKind) -> Source:
    return Source(
        source_id=source_id,
        name=source_id.upper(),
        kind=kind,
        url=f"https://x.com/{source_id}" if kind is SourceKind.X else f"https://e/{source_id}",
        url_hash=f"h-{source_id}",
        group_id="g",
        added_at="2026-07-01T00:00:00+00:00",
    )


def _x_item(tmp_path: Path) -> Item:
    """Produce a real X item the way ingest would: fetch via XFetcher + normalize."""
    tweets = {
        "karpathy": [
            Tweet(
                id="1799000000000000001",
                url="https://x.com/karpathy/status/1799000000000000001",
                text="nanoGPT speedrun: GPT-2 reproduced in 90 minutes",
                author="karpathy",
                created_at="2026-07-08T15:00:00+00:00",
                lang="en",
            )
        ]
    }
    source = make_source("x-karpathy", kind=SourceKind.X, url="https://x.com/karpathy")
    raw = XFetcher(FakeTweetSource(tweets)).fetch(source)
    return normalize_batch(raw, source, fetched_at="2026-07-08T16:00:00+00:00")[0]


def _queries(tmp_path: Path) -> tuple[TrendQueries, Item]:
    repo = JsonlSqliteRepository(tmp_path)
    x_item = _x_item(tmp_path)
    rss_item = Item(
        item_id="rss1",
        source_id="rss-src",
        kind=SourceKind.RSS,
        external_id="rss1",
        canonical_url="https://example.com/rss1",
        title="An article about nanoGPT",
        summary="s",
        published_at="2026-07-08T09:00:00+00:00",
        fetched_at="2026-07-08T10:00:00+00:00",
        content_hash="h",
    )
    repo.add_items([x_item, rss_item])
    repo.add_item_keywords([_kw(x_item.item_id, "nanogpt"), _kw("rss1", "nanogpt")])
    repo.load_config([], [_source("x-karpathy", SourceKind.X), _source("rss-src", SourceKind.RSS)])
    repo.rebuild_cache()
    repo.close()
    rules = KeywordRules.from_config(KeywordsConfig())
    return TrendQueries(open_cache(DataLayout(tmp_path)), rules), x_item


def test_tweet_text_becomes_the_item_title(tmp_path: Path) -> None:
    x_item = _x_item(tmp_path)
    assert x_item.kind is SourceKind.X
    assert x_item.title == "nanoGPT speedrun: GPT-2 reproduced in 90 minutes"
    assert x_item.external_id == "1799000000000000001"


def test_x_gets_its_own_keyword_tab(tmp_path: Path) -> None:
    queries, x_item = _queries(tmp_path)
    details = queries.keyword_details(window_ending_at(_NOW, days=30), min_mentions=1)
    nanogpt = details["nanogpt"]
    # kind tab present for X, alongside rss (sorted tab order).
    assert "x" in nanogpt.kinds
    assert [i.item_id for i in nanogpt.items_by_kind["x"]] == [x_item.item_id]
    # the site shows the tweet text (carried as title).
    assert nanogpt.items_by_kind["x"][0].title.startswith("nanoGPT speedrun")
