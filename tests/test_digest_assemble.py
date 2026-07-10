"""Digest input assembler tests (GRP-40): category scope, rising, determinism.

Builds a small JSONL truth with two categories, rebuilds the cache, and drives
:func:`assemble_digest_input` for a daily period. Covers category scoping (a
different category's items never leak in), distinct-item counts, rising
detection vs the previous window, top-item selection, and determinism.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from grepify.config.schemas import DigestSettings, KeywordsConfig
from grepify.digest.assemble import assemble_digest_input
from grepify.digest.periods import previous_day
from grepify.keywords import KeywordRules
from grepify.models import (
    DigestKind,
    ExtractionMethod,
    Item,
    ItemKeyword,
    Source,
    SourceGroup,
    SourceKind,
)
from grepify.paths import DataLayout
from grepify.repository import JsonlSqliteRepository
from grepify.site.trends import TrendQueries, open_cache

# Daily period for a summer clock: previous Edmonton day = 2026-07-07
# (window [2026-07-07T06:00Z, 2026-07-08T06:00Z)); previous window is 2026-07-06.
_NOW = datetime(2026, 7, 8, 13, 0, tzinfo=UTC)
_CUR = "2026-07-07T18:00:00+00:00"  # inside the current day window
_PREV = "2026-07-06T18:00:00+00:00"  # inside the previous day window


def _item(item_id: str, *, source_id: str, published_at: str) -> Item:
    return Item(
        item_id=item_id,
        source_id=source_id,
        kind=SourceKind.RSS,
        external_id=item_id,
        canonical_url=f"https://example.com/{item_id}",
        title=f"title {item_id}",
        summary=f"summary {item_id}",
        published_at=published_at,
        fetched_at=_CUR,
        content_hash=f"hash-{item_id}",
    )


def _kw(item_id: str, keyword: str, rank: int = 1) -> ItemKeyword:
    return ItemKeyword(
        item_id=item_id,
        keyword=keyword,
        rank=rank,
        method=ExtractionMethod.LLM,
        model="m",
        extracted_at=_CUR,
    )


def _source(source_id: str, group_id: str) -> Source:
    return Source(
        source_id=source_id,
        name=source_id.upper(),
        kind=SourceKind.RSS,
        url=f"https://example.com/{source_id}/feed",
        url_hash=f"urlhash-{source_id}",
        group_id=group_id,
        added_at="2026-07-01T00:00:00+00:00",
    )


def _queries(tmp_path: Path) -> TrendQueries:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items(
        [
            _item("i1", source_id="s1", published_at=_CUR),
            _item("i2", source_id="s1", published_at=_CUR),
            _item("i3", source_id="s2", published_at=_CUR),
            _item("p1", source_id="s1", published_at=_PREV),  # previous window
            _item("d1", source_id="sd", published_at=_CUR),  # data-eng category
        ]
    )
    repo.add_item_keywords(
        [
            _kw("i1", "genai"),
            _kw("i1", "agents", rank=2),
            _kw("i2", "genai"),
            _kw("i3", "genai"),
            _kw("p1", "genai"),  # previous-window mention (rising baseline)
            _kw("d1", "genai"),  # different category - must be excluded
        ]
    )
    repo.load_config(
        [
            SourceGroup(group_id="g-ai", name="AI", category="ai"),
            SourceGroup(group_id="g-de", name="Data", category="data-eng"),
        ],
        [_source("s1", "g-ai"), _source("s2", "g-ai"), _source("sd", "g-de")],
    )
    repo.rebuild_cache()
    repo.close()
    conn = open_cache(DataLayout(tmp_path))
    return TrendQueries(conn, KeywordRules.from_config(KeywordsConfig()))


def test_assemble_scopes_to_category_and_flags_rising(tmp_path: Path) -> None:
    di = assemble_digest_input(
        _queries(tmp_path),
        category="ai",
        kind=DigestKind.DAILY,
        period=previous_day(_NOW),
        settings=DigestSettings(),
    )
    assert di.category == "ai"
    assert di.item_count == 3  # i1,i2,i3 - the data-eng item d1 is excluded
    by_kw = {b.keyword: b for b in di.keywords}
    assert set(by_kw) == {"genai", "agents"}
    assert by_kw["genai"].count == 3
    assert by_kw["genai"].previous_count == 1  # p1 in the prior day
    assert by_kw["genai"].rising is True  # 3 >= 3 and 3/1 >= 3.0
    assert by_kw["agents"].count == 1
    assert by_kw["agents"].rising is False
    assert di.rising_keywords == ["genai"]


def test_assemble_top_items_are_capped_and_scoped(tmp_path: Path) -> None:
    di = assemble_digest_input(
        _queries(tmp_path),
        category="ai",
        kind=DigestKind.DAILY,
        period=previous_day(_NOW),
        settings=DigestSettings(items_per_keyword=2),
    )
    genai = next(b for b in di.keywords if b.keyword == "genai")
    assert len(genai.items) == 2  # capped at items_per_keyword
    assert all(item.item_id in {"i1", "i2", "i3"} for item in genai.items)  # scoped + in-window


def test_assemble_is_deterministic(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    period = previous_day(_NOW)
    first = assemble_digest_input(
        q, category="ai", kind=DigestKind.DAILY, period=period, settings=DigestSettings()
    )
    second = assemble_digest_input(
        q, category="ai", kind=DigestKind.DAILY, period=period, settings=DigestSettings()
    )
    assert first == second
