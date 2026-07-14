"""Page-helper tests (GRP-32/33/34): collapse, pagination, filter predicate."""

from __future__ import annotations

import pytest

from grepify.health import HealthSnapshot, SourceHealth
from grepify.models import FetchStatus, Rung, Source, SourceKind, SourceStatus
from grepify.site.pages import (
    build_health_view,
    build_pages,
    collapse_near_duplicates,
    item_matches_filter,
    latest_digest_per_category,
    page_facets,
    paginate,
    rising_strip,
)
from grepify.site.trends import CloudDataset, DigestDetail, ItemSummary, KeywordCount
from grepify.windows import Window
from tests.conftest import make_source


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


# --- latest digest per category ----------------------------------------------


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
        model="digest-model",
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


# --- rising strip --------------------------------------------------------------


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


# --- health view lifecycle split (ADR 0002 §2, pinned health ACs; GRP-66) ----


def _src(source_id: str, status: SourceStatus, **extra: object):  # type: ignore[no-untyped-def]
    return make_source(source_id).model_copy(update={"status": status, **extra})


def _sh(source_id: str, status: FetchStatus, *, rung: Rung | None = None, flagged: bool = False):  # type: ignore[no-untyped-def]
    return SourceHealth(
        source_id=source_id,
        attempts=18,
        last_status=status,
        last_started_at="2026-07-08T08:00:00+00:00",
        consecutive_failures=18 if flagged else 0,
        flagged=flagged,
        last_rung=rung,
    )


def test_health_view_splits_live_from_disabled() -> None:
    sources = [
        _src("a", SourceStatus.ACTIVE),
        _src("d", SourceStatus.DEGRADED),
        _src("dead", SourceStatus.DEAD, evidence="e"),
        _src("pay", SourceStatus.PAYWALLED, message="m"),
    ]
    snap = HealthSnapshot(run_id="r", generated_at="t", sources=[])
    view = build_health_view(snap, sources)
    assert {r.source_id for r in view.live} == {"a", "d"}
    assert {r.source_id for r in view.disabled} == {"dead", "pay"}


def test_health_view_drops_rows_for_sources_no_longer_in_config() -> None:
    # A `gone` source is removed from config; its stale fetch-log row must not
    # linger on the health page.
    snap = HealthSnapshot(
        run_id="r", generated_at="t", sources=[_sh("ghost", FetchStatus.ERROR, flagged=True)]
    )
    view = build_health_view(snap, [_src("a", SourceStatus.ACTIVE)])
    ids = {r.source_id for r in view.live + view.disabled}
    assert "ghost" not in ids


def test_degraded_row_exposes_served_rung() -> None:
    snap = HealthSnapshot(
        run_id="r", generated_at="t", sources=[_sh("d", FetchStatus.OK, rung=Rung.AUTODISCOVERY)]
    )
    view = build_health_view(snap, [_src("d", SourceStatus.DEGRADED)])
    row = view.live[0]
    assert row.is_degraded
    assert row.rung_label == "autodiscovery"


def test_quiet_source_never_shows_flagged() -> None:
    snap = HealthSnapshot(
        run_id="r", generated_at="t", sources=[_sh("q", FetchStatus.ERROR, flagged=False)]
    )
    view = build_health_view(snap, [_src("q", SourceStatus.ACTIVE)], quiet_source_ids={"q"})
    assert view.live[0].show_flagged is False


def test_no_snapshot_reports_absent() -> None:
    view = build_health_view(None, [_src("a", SourceStatus.ACTIVE)])
    assert view.has_snapshot is False
    assert {r.source_id for r in view.live} == {"a"}


# --- coverage: last-contributed recency + quiet rollup (GRP-70) --------------

from datetime import UTC, datetime  # noqa: E402

from grepify.site.pages import build_source_rows, coverage_rollup, source_recency  # noqa: E402

_NOW = datetime(2026, 7, 8, tzinfo=UTC)


def test_source_recency_never_contributed() -> None:
    recency = source_recency(None, now=_NOW)
    assert recency.days is None
    assert recency.label == "never"


def test_source_recency_today_and_yesterday_labels() -> None:
    assert source_recency("2026-07-08T00:00:00+00:00", now=_NOW).label == "today"
    assert source_recency("2026-07-07T00:00:00+00:00", now=_NOW).label == "1 day ago"
    assert source_recency("2026-06-08T00:00:00+00:00", now=_NOW).label == "30 days ago"


def test_source_recency_clamps_future_timestamp_to_zero() -> None:
    # A last-contributed timestamp after `now` (clock skew, an odd fixture)
    # must not render as a negative day count.
    recency = source_recency("2026-07-09T00:00:00+00:00", now=_NOW)
    assert recency.days == 0
    assert recency.label == "today"


def test_build_source_rows_quiet_after_threshold_or_never() -> None:
    sources = [
        _src("fresh", SourceStatus.ACTIVE),
        _src("stale", SourceStatus.ACTIVE),
        _src("silent", SourceStatus.ACTIVE),  # never in last_contributed
    ]
    last_contributed = {
        "fresh": "2026-07-07T00:00:00+00:00",  # 1 day ago
        "stale": "2026-05-01T00:00:00+00:00",  # far past the 30d threshold
    }
    rows = build_source_rows(sources, last_contributed, now=_NOW, quiet_after_days=30)
    by_id = {r.source_id: r for r in rows}
    assert by_id["fresh"].quiet is False
    assert by_id["stale"].quiet is True
    assert by_id["silent"].quiet is True
    assert by_id["silent"].recency.label == "never"


def test_build_source_rows_never_flags_a_disabled_source_as_quiet() -> None:
    # A dead/paywalled source's silence is already explained by its lifecycle
    # class (GRP-66) - it must never also read as coverage-quiet.
    sources = [_src("dead", SourceStatus.DEAD, evidence="e")]
    rows = build_source_rows(sources, {}, now=_NOW, quiet_after_days=30)
    assert rows[0].quiet is False


def test_coverage_rollup_counts_live_only() -> None:
    sources = [
        _src("live-fresh", SourceStatus.ACTIVE),
        _src("live-quiet", SourceStatus.ACTIVE),
        _src("dead-quiet", SourceStatus.DEAD, evidence="e"),
    ]
    last_contributed = {"live-fresh": "2026-07-07T00:00:00+00:00"}
    rows = build_source_rows(sources, last_contributed, now=_NOW, quiet_after_days=30)
    rollup = coverage_rollup(rows, quiet_after_days=30)
    assert rollup.live_count == 2  # the dead source is excluded from the denominator
    assert rollup.quiet_names == ("LIVE-QUIET",)  # make_source upper-cases the id as name
    assert rollup.has_quiet is True


def test_coverage_rollup_empty_when_nothing_quiet() -> None:
    sources = [_src("a", SourceStatus.ACTIVE)]
    last_contributed = {"a": "2026-07-08T00:00:00+00:00"}
    rows = build_source_rows(sources, last_contributed, now=_NOW, quiet_after_days=30)
    rollup = coverage_rollup(rows, quiet_after_days=30)
    assert rollup.has_quiet is False
    assert rollup.quiet_names == ()


# --- health view source endpoints -------------------------------------------


def test_health_view_keeps_configured_fallback_and_observed_endpoint_separate() -> None:

    source = Source(
        source_id="s1",
        name="S1",
        kind=SourceKind.RSS,
        url="https://example.com/feed.xml",
        url_hash="abc",
        group_id="g",
        added_at="2026-07-01T00:00:00+00:00",
        status=SourceStatus.DEGRADED,
        active_url="https://configured.example/alt.xml",
    )
    snapshot = HealthSnapshot(
        run_id="r",
        generated_at="2026-07-02T00:00:00+00:00",
        sources=[
            SourceHealth(
                source_id="s1",
                attempts=1,
                last_status=FetchStatus.OK,
                last_started_at="2026-07-02T00:00:00+00:00",
                consecutive_failures=0,
                flagged=False,
                last_resolved_url="https://observed.example/found.xml",
            )
        ],
    )

    row = build_health_view(snapshot, [source]).live[0]
    assert row.configured_fallback_url == "https://configured.example/alt.xml"
    assert row.configured_fallback_link is not None
    assert row.configured_fallback_link.href == "https://configured.example/alt.xml"
    assert row.last_resolved_url == "https://observed.example/found.xml"
    assert row.last_resolved_link is not None
    assert row.last_resolved_link.href == "https://observed.example/found.xml"


def test_health_view_does_not_link_unsafe_configured_or_observed_endpoints() -> None:

    source = Source(
        source_id="s1",
        name="S1",
        kind=SourceKind.RSS,
        url="https://example.com/feed.xml",
        url_hash="abc",
        group_id="g",
        added_at="2026-07-01T00:00:00+00:00",
        status=SourceStatus.DEGRADED,
        active_url="https://user:pass@example.com/alt.xml",
    )
    snapshot = HealthSnapshot(
        run_id="r",
        generated_at="2026-07-02T00:00:00+00:00",
        sources=[
            SourceHealth(
                source_id="s1",
                attempts=1,
                last_status=FetchStatus.OK,
                last_started_at="2026-07-02T00:00:00+00:00",
                consecutive_failures=0,
                flagged=False,
                last_resolved_url="http://127.0.0.1/feed.xml",
            )
        ],
    )

    row = build_health_view(snapshot, [source]).live[0]
    assert row.configured_fallback_url == "https://user:pass@example.com/alt.xml"
    assert row.configured_fallback_link is None
    assert row.last_resolved_url == "http://127.0.0.1/feed.xml"
    assert row.last_resolved_link is None
