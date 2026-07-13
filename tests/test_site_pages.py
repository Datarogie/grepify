"""Page-helper tests (GRP-32/33/34): collapse, pagination, filter predicate."""

from __future__ import annotations

import pytest

from grepify.site.pages import (
    build_pages,
    collapse_near_duplicates,
    item_matches_filter,
    latest_digest_per_category,
    page_facets,
    paginate,
    rising_strip,
)
from grepify.site.trends import CloudDataset, DigestDetail, ItemSummary, KeywordCount, Window


def _summary(
    item_id: str, *, content_hash: str, kind: str = "rss", source_id: str = "s1"
) -> ItemSummary:
    return ItemSummary(
        item_id=item_id,
        source_id=source_id,
        source_name=source_id.upper(),
        kind=kind,
        title=f"title {item_id}",
        canonical_url=f"https://example.com/{item_id}",
        published_at="2026-07-07T10:00:00+00:00",
        summary=None,
        content_hash=content_hash,
    )


# --- near-dup collapse -------------------------------------------------------


def test_collapse_groups_near_duplicate_titles() -> None:
    items = [
        _summary("i1", content_hash="aaaaaaaaaaaaaaaa"),
        _summary("i2", content_hash="aaaaaaaaaaaaaaab"),  # 1 bit off i1 → grouped
        _summary("i3", content_hash="ffffffffffffffff"),  # far → its own group
    ]
    groups = collapse_near_duplicates(items, max_distance=3)
    assert len(groups) == 2
    assert groups[0].representative.item_id == "i1"
    assert [d.item_id for d in groups[0].similar] == ["i2"]
    assert groups[0].similar_count == 1
    assert groups[1].representative.item_id == "i3"
    assert groups[1].similar == []


def test_collapse_preserves_input_order_of_representatives() -> None:
    # newest-first input → representatives stay newest-first
    items = [_summary(f"i{n}", content_hash=f"{n:016x}") for n in (5, 4, 3)]
    groups = collapse_near_duplicates(items, max_distance=0)
    assert [g.representative.item_id for g in groups] == ["i5", "i4", "i3"]


# --- pagination --------------------------------------------------------------


def test_paginate_splits_and_flags() -> None:
    groups = collapse_near_duplicates(
        [_summary(f"i{n}", content_hash=f"{n:016x}") for n in range(45)], max_distance=0
    )
    pages = paginate(groups, per_page=20)
    assert [p.number for p in pages] == [1, 2, 3]
    assert [len(p.groups) for p in pages] == [20, 20, 5]
    assert pages[0].total_pages == 3
    assert pages[0].has_prev is False and pages[0].has_next is True
    assert pages[1].has_prev is True and pages[1].has_next is True
    assert pages[2].has_prev is True and pages[2].has_next is False


def test_paginate_empty_is_one_empty_page() -> None:
    pages = paginate([], per_page=20)
    assert len(pages) == 1
    assert pages[0].groups == []
    assert pages[0].has_prev is False and pages[0].has_next is False


def test_paginate_rejects_nonpositive_per_page() -> None:
    with pytest.raises(ValueError, match="positive"):
        paginate([], per_page=0)


# --- build_pages: paginate raw items, collapse per page (O(n), not O(n²)) ----


def test_build_pages_paginates_by_raw_item_count() -> None:
    items = [_summary(f"i{n}", content_hash=f"{n:016x}") for n in range(45)]
    pages = build_pages(items, per_page=20, max_distance=0)
    assert [p.number for p in pages] == [1, 2, 3]
    # 20 raw items/page → (with no collapse) 20 groups/page
    assert [len(p.groups) for p in pages] == [20, 20, 5]


def test_build_pages_collapses_within_a_page() -> None:
    # two near-dups + one distinct, all on one page → 2 groups, "1 similar"
    items = [
        _summary("i1", content_hash="0000000000000001"),
        _summary("i2", content_hash="0000000000000003"),  # ~i1
        _summary("i3", content_hash="ffffffffffffffff"),
    ]
    pages = build_pages(items, per_page=20)
    assert len(pages) == 1
    assert len(pages[0].groups) == 2
    assert pages[0].groups[0].similar_count == 1


def test_build_pages_empty_is_one_empty_page() -> None:
    pages = build_pages([], per_page=20)
    assert len(pages) == 1 and pages[0].groups == []


# --- filter predicate (pins the filters.js contract) ------------------------


def test_filter_empty_matches_everything() -> None:
    assert item_matches_filter(kind="rss", source_id="s1", keywords=["genai"])


def test_filter_kind_and_source_are_exact() -> None:
    assert item_matches_filter(
        kind="rss", source_id="s1", keywords=[], kind_filter="rss", source_filter="s1"
    )
    assert not item_matches_filter(kind="rss", source_id="s1", keywords=[], kind_filter="youtube")
    assert not item_matches_filter(kind="rss", source_id="s1", keywords=[], source_filter="s2")


def test_filter_keyword_is_case_insensitive_substring() -> None:
    assert item_matches_filter(
        kind="rss", source_id="s1", keywords=["GenAI", "llm"], keyword_filter="gen"
    )
    assert not item_matches_filter(
        kind="rss", source_id="s1", keywords=["llm"], keyword_filter="gen"
    )
    # blank/whitespace keyword filter matches everything
    assert item_matches_filter(kind="rss", source_id="s1", keywords=[], keyword_filter="   ")


def test_filter_is_conjunction() -> None:
    assert item_matches_filter(
        kind="rss",
        source_id="s1",
        keywords=["genai"],
        kind_filter="rss",
        source_filter="s1",
        keyword_filter="genai",
    )
    assert not item_matches_filter(
        kind="rss",
        source_id="s1",
        keywords=["genai"],
        kind_filter="rss",
        source_filter="s1",
        keyword_filter="agents",  # keyword misses → whole thing fails
    )


# --- facets ------------------------------------------------------------------


def test_page_facets_sorted_and_deduped() -> None:
    items = [
        _summary("i1", content_hash=f"{1:016x}", kind="rss", source_id="s2"),
        _summary("i2", content_hash=f"{2:016x}", kind="youtube", source_id="s1"),
    ]
    groups = collapse_near_duplicates(items, max_distance=0)
    page = paginate(groups)[0]
    facets = page_facets(page, {"i1": ["genai", "llm"], "i2": ["genai"]})
    assert facets["kinds"] == ["rss", "youtube"]
    assert facets["sources"] == [{"id": "s1", "name": "S1"}, {"id": "s2", "name": "S2"}]
    assert facets["keywords"] == ["genai", "llm"]


# --- latest digest per category (T4) -----------------------------------------


def _digest(
    digest_id: str,
    *,
    category: str,
    kind: str = "daily",
    period_start: str = "2026-07-07T00:00:00+00:00",
    created_at: str,
) -> DigestDetail:
    return DigestDetail(
        digest_id=digest_id,
        kind=kind,
        category=category,
        title=f"digest {digest_id}",
        body_md="body",
        top_keywords=[],
        period_start=period_start,
        period_end="2026-07-08T00:00:00+00:00",
        created_at=created_at,
    )


def test_latest_digest_per_category_keeps_newest_first_seen() -> None:
    # input already in all_digests() order: period_start desc, created_at desc,
    # digest_id desc. A catch-up run wrote "daily-ai-2026-07-07" (an OLDER
    # period) after "weekly-ai-2026-W27" (a NEWER period), so created_at
    # disagrees with period order for the "ai" pair - proving period wins.
    digests = [
        _digest(
            "weekly-ai-2026-W27",
            category="ai",
            kind="weekly",
            period_start="2026-07-07T00:00:00+00:00",
            created_at="2026-07-05T09:10:00+00:00",
        ),
        _digest(
            "daily-policy-2026-07-06",
            category="policy",
            period_start="2026-07-06T00:00:00+00:00",
            created_at="2026-07-06T09:00:00+00:00",
        ),
        _digest(
            "daily-ai-2026-07-07",
            category="ai",
            period_start="2026-07-05T00:00:00+00:00",
            created_at="2026-07-07T09:05:00+00:00",
        ),
    ]
    result = latest_digest_per_category(digests)
    assert [d.category for d in result] == ["ai", "policy"]  # sorted by category
    assert result[0].digest_id == "weekly-ai-2026-W27"  # newest-period "ai" wins
    assert result[1].digest_id == "daily-policy-2026-07-06"


def test_latest_digest_per_category_empty_input() -> None:
    assert latest_digest_per_category([]) == []


# --- rising strip (GRP-68) ---------------------------------------------------


def _cloud(*counts: tuple[str, int, int, bool]) -> CloudDataset:
    # counts: (keyword, count, delta, rising) - already in the cloud's
    # count-ranked (-count, keyword) order, as TrendQueries.cloud() produces it.
    window = Window(start="2026-07-01T00:00:00+00:00", end="2026-07-08T00:00:00+00:00", days=7)
    return CloudDataset(
        window=window,
        keywords=[
            KeywordCount(keyword=kw, count=count, delta=delta, rising=rising)
            for kw, count, delta, rising in counts
        ],
    )


def test_rising_strip_filters_out_non_rising_keywords() -> None:
    cloud = _cloud(
        ("genai", 10, 4, True),
        ("webinar", 8, 1, False),
        ("agents", 5, 5, True),
    )
    assert [kw.keyword for kw in rising_strip(cloud)] == ["genai", "agents"]


def test_rising_strip_empty_when_nothing_rising() -> None:
    cloud = _cloud(("genai", 10, 4, False), ("webinar", 8, 1, False))
    assert rising_strip(cloud) == []


def test_rising_strip_preserves_cloud_count_rank_order() -> None:
    # rising_strip must not re-sort - it inherits the cloud's already-total
    # order (-count, keyword); ties here are already broken by keyword asc.
    cloud = _cloud(
        ("zeta", 9, 9, True),
        ("alpha", 9, 9, True),  # tie on count with "zeta", but sorted after it
        ("beta", 3, 3, True),
    )
    assert [kw.keyword for kw in rising_strip(cloud)] == ["zeta", "alpha", "beta"]


def test_rising_strip_caps_at_limit() -> None:
    cloud = _cloud(*[(f"kw{n}", 10 - n, 10 - n, True) for n in range(10)])
    capped = rising_strip(cloud, limit=3)
    assert [kw.keyword for kw in capped] == ["kw0", "kw1", "kw2"]


def test_rising_strip_default_limit_caps_at_eight() -> None:
    cloud = _cloud(*[(f"kw{n}", 20 - n, 20 - n, True) for n in range(10)])
    assert len(rising_strip(cloud)) == 8
