"""Storage layer tests (GRP-03): idempotency + deterministic rebuild."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grepify.errors import RepositoryError
from grepify.models import (
    Digest,
    DigestKind,
    ExtractionMethod,
    FetchLogEntry,
    FetchStatus,
    Source,
    SourceGroup,
    SourceKind,
)
from grepify.repository import JsonlSqliteRepository
from tests.conftest import make_item, make_keyword


def _rows(db: Path, sql: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


def test_add_items_is_idempotent(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    items = [make_item(f"i{n}") for n in range(3)]

    assert repo.add_items(items) == 3
    assert repo.add_items(items) == 0
    assert repo.add_items([make_item("i0"), make_item("i9")]) == 1  # only i9 is new

    repo.rebuild_cache()
    assert repo.count_items() == 4
    repo.close()


def test_items_partitioned_by_published_date(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items([make_item("a", published_at="2026-07-07T10:00:00+00:00")])
    assert (tmp_path / "items" / "2026" / "07" / "07.jsonl").exists()


def test_rebuild_is_deterministic(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items([make_item(f"i{n}") for n in range(5)])
    repo.add_item_keywords([make_keyword(f"i{n}", f"kw{n}") for n in range(5)])

    # No `order by`: rely on insertion (rowid) order so the assertion catches a
    # nondeterministic insert order, not just same-set membership.
    repo.rebuild_cache()
    first = _rows(tmp_path / "grepify.db", "select item_id, title from items")
    repo.rebuild_cache()
    second = _rows(tmp_path / "grepify.db", "select item_id, title from items")

    assert first == second
    assert [r[0] for r in first] == [f"i{n}" for n in range(5)]
    assert len(first) == 5
    repo.close()


def test_keywords_idempotent_on_composite_key(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    kws = [make_keyword("i1", "genai"), make_keyword("i1", "llm", rank=2)]
    assert repo.add_item_keywords(kws) == 2
    assert repo.add_item_keywords(kws) == 0
    repo.rebuild_cache()
    assert repo.count_item_keywords() == 2
    repo.close()


def test_keyword_key_includes_method_llm_and_fallback_rows_coexist(tmp_path: Path) -> None:
    # PRD §6: method joins the primary key so an llm
    # row and a fallback row with identical keyword text are both truth, not
    # a duplicate write - this is what lets backfill converge (test_backfill.py).
    repo = JsonlSqliteRepository(tmp_path)
    fallback_row = make_keyword("i1", "ai").model_copy(
        update={"method": ExtractionMethod.FALLBACK, "model": None}
    )
    llm_row = make_keyword("i1", "ai")  # method='llm' by default
    assert repo.add_item_keywords([fallback_row]) == 1
    assert repo.add_item_keywords([llm_row]) == 1  # same (item_id, keyword), different method
    assert repo.add_item_keywords([llm_row]) == 0  # exact duplicate still skipped
    repo.rebuild_cache()
    assert repo.count_item_keywords() == 2
    repo.close()


def test_rewrite_items_overwrites_in_place(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    a = make_item("a", published_at="2026-07-07T10:00:00+00:00")
    b = make_item("b", published_at="2026-07-07T11:00:00+00:00")  # same day file
    repo.add_items([a, b])

    rewritten = repo.rewrite_items([a.model_copy(update={"summary": "clean text"})])
    assert rewritten == 1

    by_id = {i.item_id: i for i in repo.iter_items()}
    assert by_id["a"].summary == "clean text"
    assert by_id["b"].summary == "a summary"  # sibling in the same file untouched
    assert len(by_id) == 2  # no row appended
    # still in its published-date partition, not moved
    assert (tmp_path / "items" / "2026" / "07" / "07.jsonl").exists()
    repo.close()


def test_rewrite_items_skips_unknown_and_empty(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items([make_item("a")])
    assert repo.rewrite_items([]) == 0
    # an item_id not already in truth is skipped, never appended
    assert repo.rewrite_items([make_item("ghost")]) == 0
    assert {i.item_id for i in repo.iter_items()} == {"a"}
    repo.close()


def test_delete_item_keywords_removes_only_targeted(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_item_keywords(
        [make_keyword("a", "genai"), make_keyword("a", "llm", rank=2), make_keyword("b", "agents")]
    )
    deleted = repo.delete_item_keywords(["a"])
    assert deleted == 2
    remaining = list(repo.iter_item_keywords())
    assert [(k.item_id, k.keyword) for k in remaining] == [("b", "agents")]
    repo.rebuild_cache()
    assert repo.count_item_keywords() == 1
    repo.close()


def test_delete_item_keywords_emptying_a_partition_removes_the_file(tmp_path: Path) -> None:
    # Deleting every row in a partition should remove the file, not leave a
    # 0-byte one; the rewrite is atomic (temp file + replace).
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_item_keywords([make_keyword("a", "genai")])
    day_file = tmp_path / "keywords" / "2026" / "07" / "07.jsonl"
    assert day_file.exists()

    assert repo.delete_item_keywords(["a"]) == 1
    assert not day_file.exists()
    assert list(repo.iter_item_keywords()) == []
    repo.close()


def test_delete_item_keywords_noops_on_empty_or_missing(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_item_keywords([make_keyword("a", "genai")])
    assert repo.delete_item_keywords([]) == 0
    assert repo.delete_item_keywords(["ghost"]) == 0
    assert len(list(repo.iter_item_keywords())) == 1
    repo.close()


def test_load_config_projects_into_cache(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    group = SourceGroup(group_id="g1", name="G1", category="ai")
    source = Source(
        source_id="s1",
        name="S1",
        kind=SourceKind.RSS,
        url="https://example.com/feed",
        url_hash="deadbeef",
        group_id="g1",
        added_at="2026-07-07T00:00:00+00:00",
    )
    repo.load_config([group], [source])
    repo.rebuild_cache()

    assert _rows(tmp_path / "grepify.db", "select group_id from source_groups") == [("g1",)]
    assert _rows(tmp_path / "grepify.db", "select source_id, enabled from sources") == [("s1", 1)]
    repo.close()


def test_digest_and_fetch_log_round_trip(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_digest(
        Digest(
            digest_id="daily-ai-2026-07-07",
            kind=DigestKind.DAILY,
            category="ai",
            period_start="2026-07-06T00:00:00+00:00",
            period_end="2026-07-07T00:00:00+00:00",
            title="Daily AI",
            body_md="body",
            top_keywords="[]",
            model="test-model",
            prompt_version="digest-v1",
            created_at="2026-07-07T13:00:00+00:00",
        )
    )
    repo.log_fetch(
        FetchLogEntry(
            source_id="s1",
            run_id="run-1",
            started_at="2026-07-07T09:00:00+00:00",
            status=FetchStatus.OK,
            items_new=3,
        )
    )
    repo.rebuild_cache()
    assert _rows(tmp_path / "grepify.db", "select count(*) from digests") == [(1,)]
    assert _rows(tmp_path / "grepify.db", "select items_new from fetch_log") == [(3,)]
    repo.close()


def test_iter_fetch_log_preserves_append_order_on_timestamp_ties(tmp_path: Path) -> None:
    """GRP-16: two attempts landing in the same wall-clock second (started_at
    is second-precision text) must still come back in true call order, not
    reordered by a tie-break on something like `run_id`, which ends in random
    entropy and has no relation to real chronology. Health-snapshot's
    trailing-consecutive-failure computation depends on this (grepify.health).
    """
    repo = JsonlSqliteRepository(tmp_path)
    same_started_at = "2026-07-08T12:00:00+00:00"
    # run_id chosen so an alphabetic tie-break would invert the real call order.
    repo.log_fetch(
        FetchLogEntry(
            source_id="s1",
            run_id="zzz-run",
            started_at=same_started_at,
            status=FetchStatus.ERROR,
            error="boom",
        )
    )
    repo.log_fetch(
        FetchLogEntry(
            source_id="s1",
            run_id="aaa-run",
            started_at=same_started_at,
            status=FetchStatus.OK,
            items_new=1,
        )
    )

    entries = list(repo.iter_fetch_log())
    assert [e.run_id for e in entries] == ["zzz-run", "aaa-run"]
    repo.close()


def test_count_before_rebuild_raises(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    with pytest.raises(RepositoryError):
        repo.count_items()


def test_corrupt_truth_file_raises(tmp_path: Path) -> None:
    repo = JsonlSqliteRepository(tmp_path)
    repo.add_items([make_item("a")])
    bad = tmp_path / "items" / "2026" / "07" / "07.jsonl"
    bad.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(RepositoryError):
        repo.existing_item_ids()


def test_old_fetch_log_rows_ignore_new_acquisition_trace_field_absence(tmp_path):
    entry = FetchLogEntry.model_validate(
        {
            "source_id": "old",
            "run_id": "r1",
            "started_at": "2026-07-14T00:00:00+00:00",
            "status": "ok",
            "items_new": 0,
        }
    )
    assert entry.status is FetchStatus.OK
    assert entry.acquisition_trace is None


def test_fetch_log_rows_ignore_future_additive_fields(tmp_path):
    entry = FetchLogEntry.model_validate(
        {
            "source_id": "old",
            "run_id": "r1",
            "started_at": "2026-07-14T00:00:00+00:00",
            "status": "error",
            "acquisition_trace": "[]",
            "future": "ignored",
        }
    )
    assert entry.acquisition_trace == "[]"
