"""Trend queries tests (GRP-31): cloud/stats/sources on a canned DB.

Builds a small JSONL truth via :class:`JsonlSqliteRepository`, rebuilds the
cache, and drives :class:`TrendQueries` against it - the "canned DB" the AC
calls for. Covers alias/mute merge, distinct-item counting (llm + fallback rows
for the same keyword count once), deltas vs the previous window, and
determinism (identical results twice in a row). Window arithmetic itself is
covered by ``tests/test_windows.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from grepify.clock import from_iso, to_iso
from grepify.config.schemas import KeywordsConfig
from grepify.keywords import KeywordRules
from grepify.models import (
    Digest,
    DigestKind,
    ExtractionMethod,
    Item,
    ItemKeyword,
    Source,
    SourceKind,
)
from grepify.paths import DataLayout
from grepify.repository import JsonlSqliteRepository
from grepify.site.trends import TrendQueries, open_cache
from grepify.windows import window_ending_at

# 07-08 07:00 MDT: the window ends at the most recent Edmonton midnight
# (2026-07-08T06:00Z), so current is [2026-07-01, 2026-07-08) Edmonton days and
# previous is [2026-06-24, 2026-07-01) - the same items as before alignment.
_NOW = datetime(2026, 7, 8, 13, 0, tzinfo=UTC)


def _item(item_id: str, *, source_id: str, published_at: str) -> Item:
    return Item(
        item_id=item_id,
        source_id=source_id,
        kind=SourceKind.RSS,
        external_id=item_id,
        canonical_url=f"https://example.com/{item_id}",
        title=f"title {item_id}",
        summary="a summary",
        published_at=published_at,
        fetched_at="2026-07-08T11:00:00+00:00",
        content_hash=f"hash-{item_id}",
    )


def _kw(
    item_id: str,
    keyword: str,
    *,
    rank: int = 1,
    method: ExtractionMethod = ExtractionMethod.LLM,
) -> ItemKeyword:
    return ItemKeyword(
        item_id=item_id,
        keyword=keyword,
        rank=rank,
        method=method,
        model="m" if method is ExtractionMethod.LLM else None,
        extracted_at="2026-07-08T12:00:00+00:00",
    )


def _source(source_id: str) -> Source:
    return Source(
        source_id=source_id,
        name=source_id.upper(),
        kind=SourceKind.RSS,
        url=f"https://example.com/{source_id}/feed",
        url_hash=f"urlhash-{source_id}",
        group_id="g1",
        added_at="2026-07-01T00:00:00+00:00",
    )


def _canned_repo(tmp_path: Path) -> JsonlSqliteRepository:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items(
        [
            _item("i1", source_id="s1", published_at="2026-07-05T10:00:00+00:00"),  # current
            _item("i2", source_id="s1", published_at="2026-07-06T10:00:00+00:00"),  # current
            _item("i3", source_id="s2", published_at="2026-07-07T10:00:00+00:00"),  # current
            _item("i4", source_id="s2", published_at="2026-06-28T10:00:00+00:00"),  # previous
            _item("i5", source_id="s1", published_at="2026-05-01T10:00:00+00:00"),  # outside
        ]
    )
    repo.add_item_keywords(
        [
            _kw("i1", "genai"),
            _kw("i1", "llm", rank=2),
            _kw("i1", "webinar", rank=3),  # muted
            _kw("i1", "genai", method=ExtractionMethod.FALLBACK),  # same kw+item, counts once
            _kw("i2", "genai"),
            _kw("i3", "genai"),
            _kw("i3", "agents", rank=2),
            _kw("i4", "genai"),
            _kw("i4", "llm", rank=2),
            _kw("i5", "old"),
        ]
    )
    repo.load_config([], [_source("s1"), _source("s2")])
    repo.rebuild_cache()
    repo.close()
    return repo


def _rules(*, pin: tuple[str, ...] = (), extra_mute: tuple[str, ...] = ()) -> KeywordRules:
    return KeywordRules.from_config(
        KeywordsConfig(aliases={"gen ai": "genai"}, mute=["webinar", *extra_mute], pin=list(pin))
    )


def _queries(
    tmp_path: Path, *, pin: tuple[str, ...] = (), extra_mute: tuple[str, ...] = ()
) -> TrendQueries:
    _canned_repo(tmp_path)
    conn = open_cache(DataLayout(tmp_path))
    return TrendQueries(conn, _rules(pin=pin, extra_mute=extra_mute))


# --- cloud + deltas ----------------------------------------------------------


def test_cloud_counts_deltas_and_mute(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    cloud = q.cloud(window_ending_at(_NOW, days=7), rising_min_count=3, rising_ratio=3.0)
    assert [(k.keyword, k.count, k.delta) for k in cloud.keywords] == [
        ("genai", 3, 2),  # i1,i2,i3 now; i4 prev → delta +2
        ("agents", 1, 1),  # i3 now; 0 prev
        ("llm", 1, 0),  # i1 now; i4 prev → delta 0
    ]
    # 'webinar' muted; 'old' out of window
    assert all(k.keyword not in {"webinar", "old"} for k in cloud.keywords)
    assert cloud.max_count == 3
    assert cloud.min_count == 1


def test_cloud_llm_and_fallback_rows_count_item_once(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    cloud = q.cloud(window_ending_at(_NOW, days=7), rising_min_count=3, rising_ratio=3.0)
    genai = next(k for k in cloud.keywords if k.keyword == "genai")
    assert genai.count == 3


def test_cloud_limit(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    cloud = q.cloud(window_ending_at(_NOW, days=7), limit=1, rising_min_count=3, rising_ratio=3.0)
    assert [k.keyword for k in cloud.keywords] == ["genai"]


# --- pin injection -----------------------------------------------------------
#
# After alias/mute merge the window counts are genai=3, agents=1, llm=1
# (webinar muted). At limit=1 only "genai" clears the plain count cutoff, so
# that cutoff is what a pin must punch through.

_PIN_TABLE = [
    # (pin, extra_mute, expected keywords in the limit=1 cloud)
    ((), (), {"genai"}),
    (("agents",), (), {"genai", "agents"}),
    (("llm",), (), {"genai", "llm"}),
    (("agents", "llm"), (), {"genai", "agents", "llm"}),
    (("dbt",), (), {"genai"}),  # pinned but 0 mentions in window: pin invents nothing
    (("webinar",), (), {"genai"}),  # pinned but muted: mute wins
    (("agents",), ("agents",), {"genai"}),  # muted and pinned: mute wins
    (("gen ai",), (), {"genai"}),  # pinning the alias surface form, not the
    #                                canonical target, is a no-op (mute is not
    #                                alias-resolved either)
]


@pytest.mark.parametrize(("pin", "extra_mute", "expected"), _PIN_TABLE)
def test_cloud_pin_mute_alias_precedence_at_limit(
    tmp_path: Path, pin: tuple[str, ...], extra_mute: tuple[str, ...], expected: set[str]
) -> None:
    q = _queries(tmp_path, pin=pin, extra_mute=extra_mute)
    cloud = q.cloud(window_ending_at(_NOW, days=7), limit=1, rising_min_count=3, rising_ratio=3.0)
    assert {k.keyword for k in cloud.keywords} == expected


def test_cloud_pin_injected_keyword_keeps_real_count_and_delta(tmp_path: Path) -> None:
    # Pin injection folds the keyword back in with its *actual* count/delta/
    # rising - it is not a bare placeholder entry.
    q = _queries(tmp_path, pin=("agents",))
    cloud = q.cloud(window_ending_at(_NOW, days=7), limit=1, rising_min_count=3, rising_ratio=3.0)
    agents = next(k for k in cloud.keywords if k.keyword == "agents")
    assert agents.count == 1  # i3 only
    assert agents.delta == 1  # 0 in the previous window
    assert agents.rising is False  # below rising_min_count=3


def test_cloud_pin_result_stays_sorted_by_count_then_keyword(tmp_path: Path) -> None:
    # The re-sort after folding pins back in preserves the same total order
    # the unpinned path uses - byte-stable regardless of how many pins fire.
    q = _queries(tmp_path, pin=("agents", "llm"))
    cloud = q.cloud(window_ending_at(_NOW, days=7), limit=1, rising_min_count=3, rising_ratio=3.0)
    assert [k.keyword for k in cloud.keywords] == ["genai", "agents", "llm"]


def test_cloud_pin_is_a_noop_when_already_within_limit(tmp_path: Path) -> None:
    q = _queries(tmp_path, pin=("genai",))
    with_pin = q.cloud(
        window_ending_at(_NOW, days=7), limit=1, rising_min_count=3, rising_ratio=3.0
    )
    assert [k.keyword for k in with_pin.keywords] == ["genai"]


# --- rising flag -------------------------------------------------------------


def test_cloud_rising_flag_uses_settings_thresholds(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    window = window_ending_at(_NOW, days=7)
    cloud = q.cloud(window, rising_min_count=3, rising_ratio=3.0)
    rising = {k.keyword: k.rising for k in cloud.keywords}
    # genai: count=3, prev=1 (i4) → ratio exactly 3.0, clears min_count → rising
    # agents: count=1, prev=0 → surged from nothing but below min_count → not rising
    # llm: count=1, prev=1 → flat → not rising
    assert rising == {"genai": True, "agents": False, "llm": False}


def test_cloud_rising_flag_surge_from_zero(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    window = window_ending_at(_NOW, days=7)
    # loosen min_count so a from-nothing surge (agents: count=1, prev=0) qualifies
    cloud = q.cloud(window, rising_min_count=1, rising_ratio=3.0)
    agents = next(k for k in cloud.keywords if k.keyword == "agents")
    assert agents.rising is True


def test_cloud_rising_flag_threshold_edge(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    window = window_ending_at(_NOW, days=7)
    # genai: count=3, prev=1 → ratio exactly 3.0; a stricter ratio flips it off
    at_threshold = q.cloud(window, rising_min_count=3, rising_ratio=3.0)
    past_threshold = q.cloud(window, rising_min_count=3, rising_ratio=3.01)
    genai_at = next(k for k in at_threshold.keywords if k.keyword == "genai")
    genai_past = next(k for k in past_threshold.keywords if k.keyword == "genai")
    assert genai_at.rising is True
    assert genai_past.rising is False


# --- stats -------------------------------------------------------------------


def test_stats(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    stats = q.stats(window_ending_at(_NOW, days=7))
    assert stats.item_count == 3  # i1,i2,i3
    assert stats.source_count == 2  # s1,s2
    assert stats.keyword_count == 3  # genai,llm,agents (webinar muted)
    assert stats.mention_count == 5  # genai x3 + llm x1 + agents x1
    assert stats.top_keyword == "genai"
    assert stats.top_source == "S1"  # s1 has 2 items, s2 has 1


# --- top sources -------------------------------------------------------------


def test_top_sources(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    sources = q.top_sources(window_ending_at(_NOW, days=7))
    assert [(s.source_id, s.name, s.count) for s in sources] == [
        ("s1", "S1", 2),
        ("s2", "S2", 1),
    ]


# --- coverage (GRP-70) --------------------------------------------------------


def test_last_contributed_at_is_all_time_max_per_source(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    contributed = q.last_contributed_at()
    # s1's newest item is i2 (i5 is outside the trend window but still counts -
    # all-time, not window-bound); s2's newest is i3.
    assert contributed["s1"] == "2026-07-06T10:00:00+00:00"
    assert contributed["s2"] == "2026-07-07T10:00:00+00:00"
    assert "s3" not in contributed  # zero items ever -> absent, not a sentinel


# --- latest lists ------------------------------------------------------------


def test_latest_items_ordering_and_since(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    latest = q.latest_items()
    assert [i.item_id for i in latest] == ["i3", "i2", "i1", "i4", "i5"]
    assert latest[0].source_name == "S2"

    windowed = q.latest_items(since="2026-07-01T00:00:00+00:00")
    assert [i.item_id for i in windowed] == ["i3", "i2", "i1"]


def test_latest_digests_empty(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    assert q.latest_digests() == []


def _digest(
    digest_id: str,
    *,
    kind: DigestKind = DigestKind.DAILY,
    period_start: str,
    created_at: str,
) -> Digest:
    # period_end is never asserted on here, one day past period_start is enough
    period_end = to_iso(from_iso(period_start) + timedelta(days=1))
    return Digest(
        digest_id=digest_id,
        kind=kind,
        category="ai",
        period_start=period_start,
        period_end=period_end,
        title=f"digest {digest_id}",
        body_md="body",
        top_keywords=json.dumps([]),
        model="m",
        prompt_version="digest-v1",
        created_at=created_at,
    )


def test_all_digests_and_latest_digests_order_by_period_not_created_at(tmp_path: Path) -> None:
    """A catch-up run (T3, #24) can write several periods' digests with a
    near-identical ``created_at`` - here an OLDER-period digest is written with
    a LATER ``created_at`` than a NEWER-period digest written earlier in the
    same run. Both queries must still read newest-period-first."""
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_digest(
        _digest(
            "weekly-ai-2026-W27",
            kind=DigestKind.WEEKLY,
            period_start="2026-07-07T00:00:00+00:00",  # newer period
            created_at="2026-07-05T09:00:00+00:00",  # earlier created_at
        )
    )
    repo.add_digest(
        _digest(
            "daily-ai-2026-06-20",
            period_start="2026-06-20T00:00:00+00:00",  # older period
            created_at="2026-07-08T09:00:00+00:00",  # later created_at (catch-up)
        )
    )
    repo.load_config([], [])
    repo.rebuild_cache()
    repo.close()

    conn = open_cache(DataLayout(tmp_path))
    q = TrendQueries(conn, _rules())

    assert [d.digest_id for d in q.all_digests()] == [
        "weekly-ai-2026-W27",
        "daily-ai-2026-06-20",
    ]
    assert [d.digest_id for d in q.latest_digests()] == [
        "weekly-ai-2026-W27",
        "daily-ai-2026-06-20",
    ]


def test_distinct_keywords_for_items(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    tags = q.distinct_keywords_for_items(["i1", "i3"])
    assert tags == {"i1": ["genai", "llm"], "i3": ["agents", "genai"]}  # webinar muted


# --- determinism -------------------------------------------------------------


def test_queries_are_deterministic(tmp_path: Path) -> None:
    q = _queries(tmp_path)
    window = window_ending_at(_NOW, days=7)
    assert q.cloud(window, rising_min_count=3, rising_ratio=3.0) == q.cloud(
        window, rising_min_count=3, rising_ratio=3.0
    )
    assert q.stats(window) == q.stats(window)
    assert q.top_sources(window) == q.top_sources(window)
    assert q.latest_items() == q.latest_items()
